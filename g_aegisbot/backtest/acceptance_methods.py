"""Executable definitions of the calculation methods in docs/STRATEGY_ACCEPTANCE.md.

Pure functions, no I/O, no network. They exist so that the protocol's counting and statistics rules are written once, in
code, and tested; the acceptance tooling (Phase A4/A5) must call these and not re-implement them. Nothing here decides
whether a strategy passes: it only computes the numbers the protocol's criteria are defined on.

Sections: trade lifecycles and period boundaries, walk-forward windows, funding ratio, concentration, regimes, and the
block bootstrap (equity-path reconstruction, drawdown, trade statistics, paired comparison).
"""
from __future__ import annotations

import math
from datetime import timedelta

import numpy as np
import pandas as pd

QTY_EPS = 1e-12
END_OF_DATA = "end-of-data"          # the simulator's reason for a position force-closed at the end of the measured period
WINDOW_DAYS = 90
CONFIDENCE = 0.983                   # provisional one-sided level (0.05 divided by 3 candidates, rounded)
BLOCK_LENGTHS = (5, 10, 20, 30)      # 10 is the reference; the others are the sensitivity check
REFERENCE_BLOCK = 10
MIN_REGIME_TRADES = 30
FUNDING_RATIO_LIMIT = 0.25
TOP_TRADE_FRACTION = 0.05


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


# =============================================================================== trade lifecycles
def aggregate_lifecycles(fills: list[dict]) -> list[dict]:
    """Group fills into position lifecycles. One lifecycle is ONE trade, however many fills it has.

    A fill is {symbol, ts, qty, price, fee} with qty signed (+ buy, - sell) and fee >= 0. Per symbol, in time order (equal
    times keep the given order), a lifecycle starts at the first fill that moves the position away from flat and ends at the
    fill that returns it to flat. Everything in between belongs to it:
      * partial entries (scale-in) and partial exits (scale-out) are aggregated, never counted as separate trades;
      * the average entry is the quantity-weighted mean of the entries; gross PnL is realised on every reduction;
      * a fill that crosses through flat is split at flat: the part up to flat closes the lifecycle, the rest opens a new
        one, and the fee is divided in proportion to quantity;
      * entering again after the position went flat is a NEW trade;
      * positions in different symbols are separate trades even when they overlap in time.
    A lifecycle still open after the last fill has status 'open' and closed_at None. Malformed fills raise ValueError."""
    out: list[dict] = []
    by_symbol: dict[str, list] = {}
    for i, f in enumerate(fills):
        by_symbol.setdefault(f["symbol"], []).append((i, f))
    for symbol, rows in by_symbol.items():
        rows.sort(key=lambda t: (_ts(t[1]["ts"]), t[0]))
        pos, avg, cur = 0.0, 0.0, None
        for _, f in rows:
            qty, price, fee, ts = float(f["qty"]), float(f["price"]), float(f.get("fee", 0.0)), _ts(f["ts"])
            if not (math.isfinite(qty) and math.isfinite(price) and math.isfinite(fee)) or qty == 0 or fee < 0:
                raise ValueError(f"malformed fill for {symbol}: {f}")
            total = abs(qty)
            while abs(qty) > QTY_EPS:
                part_fee = fee * abs(qty) / total
                if abs(pos) <= QTY_EPS:                                   # flat: open a lifecycle with what is left
                    pos, avg = qty, price
                    cur = {"symbol": symbol, "side": "buy" if qty > 0 else "sell", "opened_at": ts, "closed_at": None,
                           "gross_pnl": 0.0, "fees": part_fee, "fills": 1, "max_abs_qty": abs(qty), "status": "open"}
                    out.append(cur)
                    qty = 0.0
                elif (qty > 0) == (pos > 0):                              # scale in
                    avg = (avg * abs(pos) + price * abs(qty)) / (abs(pos) + abs(qty))
                    pos += qty
                    cur["fees"] += part_fee
                    cur["fills"] += 1
                    cur["max_abs_qty"] = max(cur["max_abs_qty"], abs(pos))
                    qty = 0.0
                else:                                                     # reduce, possibly through flat
                    closing = min(abs(qty), abs(pos))
                    cur["gross_pnl"] += closing * (price - avg) * (1 if pos > 0 else -1)
                    cur["fees"] += fee * closing / total
                    cur["fills"] += 1
                    pos -= math.copysign(closing, pos)
                    qty -= math.copysign(closing, qty)
                    if abs(pos) <= QTY_EPS:
                        pos, cur["closed_at"], cur["status"] = 0.0, ts, "closed"
    for t in out:
        t["net_pnl"] = t["gross_pnl"] - t["fees"]
    return sorted(out, key=lambda t: (t["opened_at"], t["symbol"]))


def split_by_period(trades: list[dict], start, end) -> dict[str, list[dict]]:
    """Which trades count for the measured period [start, end).

    A trade belongs to the period if it was OPENED inside it (entry_time, or opened_at for fill lifecycles).
      excluded_before  opened before start: not counted anywhere (it belongs to an earlier period or to warm-up)
      excluded_after   opened at or after end: not counted
      completed        opened inside and closed by the strategy (stop, trailing stop, strategy exit, liquidation) inside
                       the period: these are THE trades for sample-size and per-trade statistics
      truncated        opened inside but still open at the end, force-closed by the simulator at the period's last close
                       (reason 'end-of-data') or an open fill lifecycle: not counted as completed trades, but their
                       realised-at-close PnL stays in every PnL total, so nothing is hidden
    """
    s, e = _ts(start), _ts(end)
    out = {"completed": [], "truncated": [], "excluded_before": [], "excluded_after": []}
    for t in trades:
        opened = _ts(t.get("entry_time", t.get("opened_at")))
        if opened < s:
            out["excluded_before"].append(t)
        elif opened >= e:
            out["excluded_after"].append(t)
        elif _is_truncated(t, e):
            out["truncated"].append(t)
        else:
            out["completed"].append(t)
    return out


def _is_truncated(t: dict, end: pd.Timestamp) -> bool:
    """Simulator trades say so with their exit reason (an exit stamped at the period's end is a normal stop on the last bar
    unless the reason is 'end-of-data'). Fill lifecycles are truncated when open, or closed at or after the period end."""
    if "reason" in t:
        return t["reason"] == END_OF_DATA
    closed = t.get("closed_at")
    return t.get("status") != "closed" or closed is None or _ts(closed) >= end


def _exit_key(t: dict) -> pd.Timestamp:
    """The instant used to attribute a trade to a window or a day: its exit time minus one nanosecond. The simulator stamps an
    exit with the CLOSE time of its bar, so an exit stamped exactly at midnight belongs to the day (and window) that just ended."""
    return _ts(t["exit_time"]) - pd.Timedelta(nanoseconds=1)


def net_pnl(t: dict) -> float:
    return float(t["pnl"] if "pnl" in t else t["net_pnl"])


# =============================================================================== walk-forward windows
def walk_forward_windows(start, end, days: int = WINDOW_DAYS) -> list[dict]:
    """Consecutive non-overlapping windows of exactly `days` days from `start` at 00:00 UTC. The remainder at the end, if
    shorter than `days`, is an INCOMPLETE window: it is listed (complete False), reported, and excluded from the share of
    profitable windows. Its PnL still counts in every total."""
    s, e = _ts(start), _ts(end)
    out, cur = [], s
    while cur < e:
        nxt = min(cur + timedelta(days=days), e)
        out.append({"start": cur, "end": nxt, "complete": (nxt - cur) == timedelta(days=days)})
        cur = nxt
    return out


def window_results(trades: list[dict], windows: list[dict]) -> list[dict]:
    """Net PnL per window. A trade is attributed to the window containing its EXIT time (the PnL is realised then; an exit
    stamped exactly on a boundary belongs to the window that ends there), whatever window it was opened in, from one continuous
    run: no state is reset and no position is force-closed at a window edge."""
    res = []
    for w in windows:
        ins = [t for t in trades if w["start"] <= _exit_key(t) < w["end"]]
        res.append({**w, "trades": len(ins), "net": float(sum(net_pnl(t) for t in ins))})
    return res


def profitable_window_check(results: list[dict], min_share: float = 0.60) -> dict:
    """Share of COMPLETE windows with net PnL strictly above zero (a window with no trades is not profitable). Passes when
    the count of profitable complete windows is at least ceil(min_share x complete windows). Also reports the largest single
    window's share of total positive window PnL."""
    complete = [r for r in results if r["complete"]]
    wins = sum(1 for r in complete if r["net"] > 0)
    need = math.ceil(min_share * len(complete) - 1e-12)
    total_profit = sum(r["net"] for r in results if r["net"] > 0)
    top_share = (max((r["net"] for r in results), default=0.0) / total_profit) if total_profit > 0 else None
    return {"complete_windows": len(complete), "profitable": wins, "needed": need, "passes": bool(complete) and wins >= need,
            "largest_window_share": top_share}


# =============================================================================== funding, concentration
def funding_ratio_check(trades: list[dict], limit: float = FUNDING_RATIO_LIMIT) -> dict:
    """Funding cost against gross trading profit.

    gross profit G = sum of gross PnL of the counted trades (before fees and funding). Net funding cost F = funding paid
    minus funding received (the simulator's `funding` field is positive when paid). Rules:
      * G <= 0: the criterion FAILS (there is no profit for funding to be a share of); it is never a pass;
      * F <= 0 (funding was net income): ratio 0, passes;
      * otherwise passes only if F / G <= limit."""
    g = float(sum(float(t["gross_pnl"]) for t in trades))
    f = float(sum(float(t["funding"]) for t in trades))
    if g <= 0:
        return {"gross_profit": g, "net_funding_cost": f, "ratio": None, "passes": False, "reason": "gross trading profit is not positive"}
    ratio = max(0.0, f) / g
    return {"gross_profit": g, "net_funding_cost": f, "ratio": ratio, "passes": ratio <= limit, "reason": ""}


def concentration_check(trades: list[dict], max_pair_share: float = 0.40, top_fraction: float = TOP_TRADE_FRACTION) -> dict:
    """Dependence on one pair or on a few trades. Removed trades are NOT replaced: portfolio net PnL is simply recomputed
    from the remaining trades.

      total          sum of net PnL of all counted trades
      best pair      the pair with the largest net PnL; share = its net / total (only defined when total > 0)
      without pair   total minus the best pair's net, must stay > 0
      without top    total minus the net PnL of the k best trades, k = ceil(top_fraction x number of trades), must stay > 0
    Fails whenever total <= 0."""
    n = len(trades)
    total = float(sum(net_pnl(t) for t in trades))
    by_pair: dict[str, float] = {}
    for t in trades:
        by_pair[t["symbol"]] = by_pair.get(t["symbol"], 0.0) + net_pnl(t)
    best_pair = max(by_pair, key=by_pair.get) if by_pair else None
    k = math.ceil(top_fraction * n - 1e-12) if n else 0
    top = sorted((net_pnl(t) for t in trades), reverse=True)[:k]
    out = {"trades": n, "total_net": total, "best_pair": best_pair, "best_pair_share": None, "net_without_best_pair": None,
           "removed_top_trades": k, "net_without_top_trades": None, "passes": False}
    if n == 0 or total <= 0:
        return out
    out["best_pair_share"] = by_pair[best_pair] / total
    out["net_without_best_pair"] = total - by_pair[best_pair]
    out["net_without_top_trades"] = total - float(sum(top))
    out["passes"] = bool(out["best_pair_share"] <= max_pair_share and out["net_without_best_pair"] > 0
                         and out["net_without_top_trades"] > 0)
    return out


# =============================================================================== regimes
def regime_labels(btc_daily_close: pd.Series, ema_days: int = 200, vol_days: int = 30, min_vol_history: int = 90) -> pd.Series:
    """A regime label for every UTC day, using ONLY information from completed days up to and including that day.

    trend  'up' if the day's close is above its EMA(200 days, adjust=False), else 'down'; unavailable until `ema_days` days
           of history exist.
    vol    realised volatility = standard deviation of the last 30 daily log returns. Its tercile is judged against the
           values seen so far (expanding history, at least `min_vol_history` earlier values): 'low' below the 33.3rd
           percentile, 'high' above the 66.7th, else 'mid'. No future value is used.
    label  'up-low' ... 'down-high'; days without enough history are 'unclassified'.
    A trade opened on date D must use the label of D minus one day, the last day known when it was entered."""
    close = btc_daily_close.astype(float).sort_index()
    ema = close.ewm(span=ema_days, adjust=False).mean()
    trend = np.where(close.to_numpy() > ema.to_numpy(), "up", "down")
    vol = np.log(close).diff().rolling(vol_days).std()
    labels = []
    seen: list[float] = []
    for i, (day, v) in enumerate(vol.items()):
        if i < ema_days or pd.isna(v) or len(seen) < min_vol_history:       # not enough history yet
            labels.append("unclassified")
        else:
            lo, hi = np.quantile(seen, [1 / 3, 2 / 3])
            labels.append(f"{trend[i]}-{'low' if v < lo else 'high' if v > hi else 'mid'}")
        if not pd.isna(v):
            seen.append(float(v))                      # the value joins the history only AFTER it has been classified
    return pd.Series(labels, index=close.index)


def regime_table(trades: list[dict], labels: pd.Series, min_trades: int = MIN_REGIME_TRADES) -> dict[str, dict]:
    """Trades and net expectancy per regime. EVERY regime (including 'unclassified') appears in the result. A regime with
    fewer than `min_trades` trades has status INCONCLUSIVE; it is reported, never omitted."""
    rows: dict[str, list[float]] = {name: [] for name in {"up-low", "up-mid", "up-high", "down-low", "down-mid", "down-high",
                                                          "unclassified"}}
    for t in trades:
        day = _ts(t.get("entry_time", t.get("opened_at"))).normalize().tz_localize(None) - timedelta(days=1)
        idx = labels.index.tz_localize(None) if labels.index.tz is not None else labels.index
        label = labels.iloc[idx.get_loc(day)] if day in idx else "unclassified"
        rows.setdefault(label, []).append(net_pnl(t))
    out = {}
    for name, pnl in sorted(rows.items()):
        out[name] = {"trades": len(pnl), "net_expectancy": (float(np.mean(pnl)) if pnl else None),
                     "status": "INCONCLUSIVE" if len(pnl) < min_trades else "EVALUATED"}
    return out


# =============================================================================== block bootstrap
def daily_returns(equity: pd.Series, initial: float | None = None) -> np.ndarray:
    """Daily simple returns of the marked-to-market equity curve: the last equity value of each UTC day, each day's return
    measured against the previous day's last value (the first day against `initial`, default the first equity value).
    Days with no equity row carry the previous value forward (return 0)."""
    eq = equity.copy()
    eq.index = pd.DatetimeIndex(eq.index).tz_convert("UTC") if pd.DatetimeIndex(eq.index).tz is not None else \
        pd.DatetimeIndex(eq.index).tz_localize("UTC")
    daily = eq.groupby(eq.index.normalize()).last()
    daily = daily.reindex(pd.date_range(daily.index[0], daily.index[-1], freq="D", tz="UTC")).ffill()
    base = float(equity.iloc[0] if initial is None else initial)
    prev = np.concatenate([[base], daily.to_numpy()[:-1]])
    return daily.to_numpy() / prev - 1.0


def block_indices(n: int, block_len: int, n_boot: int, seed: int) -> np.ndarray:
    """Circular moving-block resampling indices, shape (n_boot, n). Each row is made of ceil(n / block_len) blocks of
    `block_len` consecutive days starting at uniformly random days (wrapping around the end), truncated to n days."""
    if not (1 <= block_len <= n):
        raise ValueError("block length must be between 1 and the number of days")
    rng = np.random.default_rng(seed)
    blocks = -(-n // block_len)
    starts = rng.integers(0, n, size=(n_boot, blocks))
    return ((starts[:, :, None] + np.arange(block_len)[None, None, :]) % n).reshape(n_boot, -1)[:, :n]


def reconstruct_paths(returns: np.ndarray, block_len: int = REFERENCE_BLOCK, n_boot: int = 10_000, seed: int = 1,
                      initial: float = 1.0, indices: np.ndarray | None = None) -> np.ndarray:
    """Bootstrap equity PATHS, shape (n_boot, n + 1). Resampled daily returns are chained in order,
    equity[t] = initial x product(1 + r*), so a path is a real compounding equity curve with its own peaks and troughs.
    Maximum drawdown is measured on THAT path (`max_drawdown`), never taken from a statistic of the resampled returns."""
    r = np.asarray(returns, dtype=float)
    if r.ndim != 1 or len(r) == 0 or not np.all(np.isfinite(r)) or np.any(r <= -1.0):
        raise ValueError("daily returns must be a non-empty finite 1-D array above -100%")
    idx = block_indices(len(r), block_len, n_boot, seed) if indices is None else indices
    paths = initial * np.cumprod(1.0 + r[idx], axis=1)
    return np.concatenate([np.full((paths.shape[0], 1), initial), paths], axis=1)


def max_drawdown(paths: np.ndarray) -> np.ndarray:
    """Largest peak-to-trough fall as a positive fraction, per path (accepts one path or many). The running peak includes
    the starting value, so a path that only falls has drawdown equal to its total fall."""
    p = np.atleast_2d(np.asarray(paths, dtype=float))
    peak = np.maximum.accumulate(p, axis=1)
    dd = ((peak - p) / peak).max(axis=1)
    return dd if np.ndim(paths) > 1 else dd[0:1]


def lower_bound(samples: np.ndarray, confidence: float = CONFIDENCE) -> float:
    """One-sided lower confidence bound: the (1 - confidence) quantile of the bootstrap distribution."""
    return float(np.quantile(np.asarray(samples, dtype=float), 1.0 - confidence))


def upper_bound(samples: np.ndarray, confidence: float = CONFIDENCE) -> float:
    return float(np.quantile(np.asarray(samples, dtype=float), confidence))


def trade_day_aggregates(trades: list[dict], start, end) -> dict[str, np.ndarray]:
    """Per calendar day of [start, end) (UTC): number of trades EXITING that day, their net PnL, gross winners, gross losers
    (absolute). Days without trades are kept as zeros, so blocks of consecutive days keep their real spacing."""
    days = pd.date_range(_ts(start).normalize(), _ts(end).normalize() - pd.Timedelta(days=1), freq="D", tz="UTC")
    pos = {d: i for i, d in enumerate(days)}
    n, pnl, win, loss = (np.zeros(len(days)) for _ in range(4))
    for t in trades:
        d = _exit_key(t).normalize()
        if d in pos:
            x = net_pnl(t)
            n[pos[d]] += 1
            pnl[pos[d]] += x
            win[pos[d]] += max(x, 0.0)
            loss[pos[d]] += max(-x, 0.0)
    return {"n": n, "pnl": pnl, "win": win, "loss": loss}


def bootstrap_trade_stats(trades: list[dict], start, end, block_len: int = REFERENCE_BLOCK, n_boot: int = 10_000,
                          seed: int = 1) -> dict[str, np.ndarray]:
    """Block bootstrap of per-trade statistics. Whole calendar days (with the trades that exited on them) are resampled in
    blocks of consecutive days; each resample's net expectancy per trade and profit factor are recomputed from the days it
    contains. A resample with no trades has expectancy 0 and profit factor 0; one with winners and no losers has profit
    factor infinity."""
    a = trade_day_aggregates(trades, start, end)
    idx = block_indices(len(a["n"]), block_len, n_boot, seed)
    n, pnl, win, loss = (a[k][idx].sum(axis=1) for k in ("n", "pnl", "win", "loss"))
    with np.errstate(divide="ignore", invalid="ignore"):
        expectancy = np.where(n > 0, pnl / np.where(n > 0, n, 1), 0.0)
        pf = np.where(loss > 0, win / np.where(loss > 0, loss, 1), np.where(win > 0, np.inf, 0.0))
    return {"expectancy": expectancy, "profit_factor": pf}


def block_length_sensitivity(returns: np.ndarray, lengths=BLOCK_LENGTHS, n_boot: int = 2_000, seed: int = 1,
                             confidence: float = CONFIDENCE, dd_limit: float = 0.35) -> dict:
    """Does the drawdown conclusion depend on the block length? For each length, the 95th percentile of the bootstrapped
    maximum drawdown and whether it is within `dd_limit`. `stable` is True only if the verdict is the same for every length;
    a protocol decision that flips with the block length is INCONCLUSIVE."""
    rows = {}
    for L in lengths:
        if L > len(returns):
            continue
        dd = max_drawdown(reconstruct_paths(returns, L, n_boot, seed))
        p95 = float(np.quantile(dd, 0.95))
        rows[L] = {"dd_p95": p95, "within_limit": p95 <= dd_limit}
    verdicts = {r["within_limit"] for r in rows.values()}
    return {"by_block_length": rows, "stable": len(verdicts) == 1 and bool(rows)}


# ------------------------------------------------------------------ paired comparison (combined portfolio)
def sharpe(returns: np.ndarray) -> np.ndarray:
    r = np.atleast_2d(returns)
    sd = r.std(axis=1, ddof=1)
    return np.where(sd > 0, r.mean(axis=1) / np.where(sd > 0, sd, 1) * math.sqrt(365), 0.0)


def calmar(returns: np.ndarray) -> np.ndarray:
    r = np.atleast_2d(returns)
    paths = np.concatenate([np.ones((r.shape[0], 1)), np.cumprod(1 + r, axis=1)], axis=1)
    years = r.shape[1] / 365.0
    cagr = paths[:, -1] ** (1 / years) - 1
    dd = max_drawdown(paths)
    return np.where(dd > 0, cagr / np.where(dd > 0, dd, 1), 0.0)


def drawdown_reduction(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Relative reduction of the maximum drawdown of A against B: (ddB - ddA) / ddB. Positive means A drew down less."""
    pa = np.concatenate([np.ones((a.shape[0], 1)), np.cumprod(1 + a, axis=1)], axis=1)
    pb = np.concatenate([np.ones((b.shape[0], 1)), np.cumprod(1 + b, axis=1)], axis=1)
    ddb = max_drawdown(pb)
    return np.where(ddb > 0, (ddb - max_drawdown(pa)) / np.where(ddb > 0, ddb, 1), 0.0)


METRICS = {"sharpe": lambda a, b: sharpe(a) - sharpe(b), "calmar": lambda a, b: calmar(a) - calmar(b),
           "drawdown_reduction": drawdown_reduction}


def paired_improvement(portfolio: np.ndarray, component: np.ndarray, metric: str, block_len: int = REFERENCE_BLOCK,
                       n_boot: int = 10_000, seed: int = 1, confidence: float = CONFIDENCE) -> dict:
    """Paired block bootstrap of the combined portfolio against its better component on the SAME days. Both daily-return
    series (same length, same calendar days) are resampled with the SAME block indices, so market-wide days are paired. The
    statistic is the metric's difference (Sharpe, Calmar) or the drawdown reduction. `distinguishable` is True when the
    one-sided lower bound of the improvement is above zero; for drawdown_reduction the point estimate must also be at least
    10%. `metric` must be the one named in the freeze record before any confirmation result is seen."""
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {sorted(METRICS)}")
    p, c = np.asarray(portfolio, dtype=float), np.asarray(component, dtype=float)
    if p.shape != c.shape or p.ndim != 1:
        raise ValueError("the two return series must be 1-D and cover the same days")
    idx = block_indices(len(p), block_len, n_boot, seed)
    samples = METRICS[metric](p[idx], c[idx])
    point = float(METRICS[metric](p[None, :], c[None, :])[0])
    lb = lower_bound(samples, confidence)
    ok = lb > 0 and (point >= 0.10 if metric == "drawdown_reduction" else point > 0)
    return {"metric": metric, "point": point, "lower_bound": lb, "distinguishable": bool(ok)}
