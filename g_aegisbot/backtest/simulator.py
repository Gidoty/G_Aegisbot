"""Event-driven multi-pair backtest of the live strategy.

It reuses the live code paths: compute_indicators / evaluate_at for entries, compute_size for
sizing and the position and risk caps, TrailingStop for exits and update_tracker for the daily
drawdown breaker. Only the exchange is simulated.

Bar-by-bar order (matches live timing):
  1. entries decided at the previous bar's close fill at this bar's OPEN (+ slippage, taker fee)
  2. stops are tested against this bar's range (gap through the stop fills at the open)
  3. funding accrues, then the trailing stop is updated from this bar and takes effect next bar
  4. equity is marked at the close and the daily drawdown breaker is evaluated
  5. new signals are evaluated on this bar's close (skipped while the breaker is tripped)

Warm-up: no signal is evaluated for a symbol until it has `warmup_candles` (default 500, the
size of the live scanner's window) closed candles behind it, and none before `measure_start`.
Warm-up bars are replayed only so indicators converge; equity, the breaker, the reported period
and every statistic start at `measure_start`.

Assumptions (also written into the report metadata, see ASSUMPTIONS):
  * funding is a constant approximation, NOT historical Binance funding rates;
  * liquidation is a simplified flat-maintenance-margin model, NOT an exact Binance liquidation
    simulation (no margin brackets, liquidation fee, insurance fund or mark-price feed);
  * slippage is a flat per-tier assumption;
  * the breaker sees hourly closes, not intrabar dips (live checks every minute, so it can
    trip earlier).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd

from g_aegisbot.backtest.metrics import compute_metrics
from g_aegisbot.config import DAILY_DRAWDOWN_LIMIT, DEFAULT_LEVERAGE
from g_aegisbot.costs import FUNDING_RATE_8H, MAKER_FEE, TAKER_FEE, slippage_rate
from g_aegisbot.engine import risk
from g_aegisbot.engine.strategy import (
    ScanResult, StrategyParams, TrailingStop, breakout_candidates, compute_indicators, compute_size,
    evaluate_at, rank_signals,
)

MAINT_MARGIN = 0.005

ASSUMPTIONS = {
    "funding": "Constant approximation (default 0.01% per 8h charged on notional). Historical Binance "
               "funding rates are NOT used.",
    "liquidation": "Simplified model with a flat 0.5% maintenance margin. This is NOT an exact Binance "
                   "liquidation simulation: no margin brackets, liquidation fee, insurance fund or mark price.",
    "slippage": "Flat per-tier assumption (3 / 6 / 12 basis points, stop orders 1.5x). Not measured fills.",
    "fills": "Entries fill at the next bar's open; stops are tested against bar high/low; a gap through "
             "the stop fills at the open.",
    "breaker": "Evaluated on hourly closes. Live checks every minute, so live can trip earlier.",
    "fees": "Binance standard USDT-M taker fee on every order (no VIP tier or BNB discount).",
}


@dataclass
class BacktestConfig:
    initial_capital: float = 10_000.0
    leverage: int = DEFAULT_LEVERAGE
    wallet_cap: float | None = None          # None: allocate the whole equity
    taker_fee: float = TAKER_FEE
    maker_fee: float = MAKER_FEE             # carried for completeness; the bot only uses market and stop-market orders
    funding_rate_8h: float = FUNDING_RATE_8H
    slippage_scale: float = 1.0
    use_breaker: bool = True
    warmup_candles: int = 500                # closed candles a symbol needs before its first signal
    measure_start: "pd.Timestamp | str | None" = None   # first measured bar; default: bar number warmup_candles
    drawdown_limit: float = DAILY_DRAWDOWN_LIMIT
    params: StrategyParams = field(default_factory=StrategyParams)


@dataclass
class _Pos:
    symbol: str
    side: str
    amount: float
    entry: float
    margin: float
    fee_in: float
    trail: TrailingStop
    opened_at: pd.Timestamp
    conviction: str
    funding: float = 0.0

    @property
    def dir(self) -> int:
        return 1 if self.side == "buy" else -1


@dataclass
class BacktestResult:
    trades: list[dict]
    equity: pd.Series
    metrics: dict
    by_symbol: dict
    notes: dict


class Backtester:
    def __init__(self, frames: dict[str, pd.DataFrame], cfg: BacktestConfig = BacktestConfig()):
        self.cfg, self.p = cfg, cfg.params
        self.tf = pd.Timedelta(seconds=ccxt.Exchange.parse_timeframe(self.p.timeframe))
        self.ind = {s: compute_indicators(df.sort_index(), self.p) for s, df in frames.items() if len(df)}
        self.cand = {s: breakout_candidates(i) for s, i in self.ind.items()}
        self.arr = {s: {c: i[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close", "atr")}
                    for s, i in self.ind.items()}
        timeline = None
        for i in self.ind.values():
            timeline = i.index if timeline is None else timeline.union(i.index)
        self.timeline = timeline
        self.pidx = {s: i.index.get_indexer(timeline) for s, i in self.ind.items()}

        if not self.ind:
            raise ValueError("no candle data supplied")
        ms = cfg.measure_start
        if ms is None:
            if len(timeline) <= cfg.warmup_candles:
                raise ValueError(f"need more than {cfg.warmup_candles} candles to leave a measured period, "
                                 f"got {len(timeline)}")
            ms = timeline[cfg.warmup_candles]
        ms = pd.Timestamp(ms)
        self.measure_start = ms.tz_localize("UTC") if ms.tzinfo is None else ms.tz_convert("UTC")
        if self.measure_start > timeline[-1]:
            raise ValueError("measure_start is after the last candle")

    # ---- cost model
    def _fill(self, symbol: str, price: float, buying: bool, *, is_stop: bool = False) -> float:
        slip = slippage_rate(symbol, is_stop=is_stop, scale=self.cfg.slippage_scale)
        return price * (1 + slip if buying else 1 - slip)

    def _liq(self, pos: _Pos) -> float:
        lev = self.cfg.leverage
        return pos.entry * (1 - 1 / lev + MAINT_MARGIN) if pos.dir == 1 else pos.entry * (1 + 1 / lev - MAINT_MARGIN)

    # ---- main loop
    def run(self) -> BacktestResult:
        cfg, p = self.cfg, self.p
        wallet = cfg.initial_capital
        positions: dict[str, _Pos] = {}
        pending: list[tuple] = []               # (signal, plan), filled at the next bar's open
        trades: list[dict] = []
        eq_t, eq_v = [self.measure_start], [cfg.initial_capital]
        last_close: dict[str, float] = {}
        tracker, tripped_days, skips = None, set(), Counter()
        n = len(self.timeline)

        def close(pos: _Pos, exit_price: float, when: pd.Timestamp, reason: str) -> None:
            nonlocal wallet
            gross = (exit_price - pos.entry) * pos.amount * pos.dir
            fee_out = exit_price * pos.amount * cfg.taker_fee
            wallet += gross - fee_out
            trades.append({
                "symbol": pos.symbol, "side": pos.side, "conviction": pos.conviction,
                "entry_time": pos.opened_at, "exit_time": when, "entry_price": pos.entry, "exit_price": exit_price,
                "amount": pos.amount, "pnl": gross - pos.fee_in - fee_out - pos.funding,
                "fees": pos.fee_in + fee_out, "funding": pos.funding, "reason": reason,
            })
            del positions[pos.symbol]

        for k in range(n):
            t = self.timeline[k]
            t_close = t + self.tf
            measuring = t >= self.measure_start      # warm-up bars are replayed but never measured

            # 1. fill entries decided at the previous close
            still_pending = []
            for sig, plan in pending:
                s = sig.symbol
                i = self.pidx[s][k]
                if i < 0:
                    still_pending.append((sig, plan))
                    continue
                entry = self._fill(s, self.arr[s]["open"][i], sig.side == "buy")
                margin = plan.amount * entry / cfg.leverage
                fee = plan.amount * entry * cfg.taker_fee
                if wallet - sum(q.margin for q in positions.values()) < margin + fee:
                    skips["no funds at fill"] += 1
                    continue
                wallet -= fee
                positions[s] = _Pos(s, sig.side, plan.amount, entry, margin, fee,
                                    TrailingStop(sig.side, sig.stop_price, entry, p.trail_atr_mult), t, sig.conviction)
            pending = still_pending

            # 2. stops
            for s, pos in list(positions.items()):
                i = self.pidx[s][k]
                if i < 0:
                    continue
                o, h, l = self.arr[s]["open"][i], self.arr[s]["high"][i], self.arr[s]["low"][i]
                stop, reason, exit_price = pos.trail.stop_price, "stop-loss", None
                if pos.dir == 1 and l <= stop:
                    exit_price = self._fill(s, min(stop, o), False, is_stop=True)
                    liq = self._liq(pos)
                    if exit_price <= liq:
                        exit_price, reason = liq, "liquidated"
                elif pos.dir == -1 and h >= stop:
                    exit_price = self._fill(s, max(stop, o), True, is_stop=True)
                    liq = self._liq(pos)
                    if exit_price >= liq:
                        exit_price, reason = liq, "liquidated"
                if exit_price is not None:
                    close(pos, exit_price, t_close, reason)

            # 3. funding, then trail the survivors (new stop applies from the next bar)
            for s, pos in positions.items():
                i = self.pidx[s][k]
                if i < 0:
                    continue
                cost = pos.amount * self.arr[s]["close"][i] * cfg.funding_rate_8h * (self.tf.total_seconds() / 28800) * pos.dir
                wallet -= cost
                pos.funding += cost
                pos.trail.update(self.arr[s]["high"][i], self.arr[s]["low"][i], self.arr[s]["atr"][i])

            # 4. mark equity, run the breaker
            for s in self.ind:
                i = self.pidx[s][k]
                if i >= 0:
                    last_close[s] = self.arr[s]["close"][i]
            equity = wallet + sum((last_close[s] - q.entry) * q.amount * q.dir for s, q in positions.items())
            if measuring:
                eq_t.append(t_close)
                eq_v.append(equity)
            tripped = False
            if cfg.use_breaker and measuring:
                tracker, event = risk.update_tracker(tracker, equity, t_close.to_pydatetime(), cfg.drawdown_limit)
                tripped = tracker["breaker_tripped"]
                if event == "tripped":
                    tripped_days.add(tracker["date"])

            # 5. signals on this bar's close
            if not measuring or tripped or k == n - 1:
                continue
            results = []
            for s in self.ind:
                i = self.pidx[s][k]
                if (i >= cfg.warmup_candles and self.cand[s][i] and s not in positions
                        and all(s != q[0].symbol for q in pending)):   # i >= 500 also excludes i == -1 (no bar)
                    results.append(evaluate_at(s, self.ind[s], int(i), p))
            for sig in rank_signals(results):
                cap = cfg.wallet_cap if cfg.wallet_cap is not None else float("inf")
                used = sum(q.margin for q in positions.values()) + sum(pl.margin for _, pl in pending)
                plan = compute_size(
                    sig, leverage=cfg.leverage, allocated=min(cap, equity), used=used, free=equity - used,
                    open_positions=len(positions) + len(pending), p=p)
                if hasattr(plan, "reason"):
                    skips[" ".join(plan.reason.split()[:3])] += 1
                    continue
                pending.append((sig, plan))

        # close whatever is still open at the last close
        for s, pos in list(positions.items()):
            close(pos, self._fill(s, last_close[s], pos.dir == -1), self.timeline[-1] + self.tf, "end of data")
        eq_v[-1] = wallet
        equity_s = pd.Series(eq_v, index=pd.DatetimeIndex(eq_t)).groupby(level=0).last()

        by_symbol = {}
        for s in self.ind:
            ts = [x for x in trades if x["symbol"] == s]
            by_symbol[s] = {"trades": len(ts), "net_pnl": float(sum(x["pnl"] for x in ts)),
                            "win_rate": float(np.mean([x["pnl"] > 0 for x in ts])) if ts else 0.0}
        first_eligible = {}
        for s, i in self.ind.items():
            if len(i) > cfg.warmup_candles:
                first_eligible[s] = str(max(i.index[cfg.warmup_candles], self.measure_start))
            else:
                first_eligible[s] = None
        notes = {
            "measurement_period": {"start": str(self.measure_start), "end": str(self.timeline[-1] + self.tf)},
            "warmup": {"required_candles": cfg.warmup_candles, "excluded_from_statistics": True,
                       "first_signal_eligible": first_eligible},
            "data_range_including_warmup": {s: [str(i.index[0]), str(i.index[-1])] for s, i in self.ind.items()},
            "breaker_trip_days": sorted(tripped_days), "skipped_signals": dict(skips),
            "assumptions": {"taker_fee": cfg.taker_fee, "funding_rate_8h": cfg.funding_rate_8h,
                            "slippage_scale": cfg.slippage_scale, "leverage": cfg.leverage,
                            "maintenance_margin": MAINT_MARGIN, **ASSUMPTIONS},
        }
        return BacktestResult(trades, equity_s, compute_metrics(equity_s, trades, cfg.initial_capital), by_symbol, notes)
