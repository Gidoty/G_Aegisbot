"""Event-driven multi-pair backtest of the live strategies.

It reuses the live code paths: compute_indicators for the indicator frame, strategies.evaluate_symbol for BOTH strategies
(entry rules, conflict handling), strategies.exit_signal, compute_size for sizing and every portfolio limit, TrailingStop
for trailing, update_tracker for the daily drawdown breaker. Only the exchange is simulated.

Bar-by-bar order (matches live timing):
  0. a symbol whose data has ended is closed at its last close (never left open at a stale price)
  1. historical funding that settles inside this bar is charged to positions carried into it (funding_mode="historical")
  2. strategy exits decided at the previous close fill at this bar's OPEN (+ slippage, taker fee)
  3. entries decided at the previous close fill at this bar's OPEN (+ slippage, taker fee)
  4. stops are tested against this bar's range; a gap through the stop fills at the open; liquidation is checked on the same
     bar and wins if the adverse move reaches it first
  5. (funding_mode="approx") constant funding accrues, then trailing stops are updated from this bar (effective next bar)
  6. equity is marked at the close and the daily drawdown breaker is evaluated
  7. new signals and strategy exits are evaluated on this bar's close (signals skipped while the breaker is tripped)

Warm-up: no signal for a symbol until it has `warmup_candles` (default 500) closed candles behind it, and none before
`measure_start`. Warm-up bars are replayed only so indicators converge; equity, the breaker, the reported period and every
statistic start at `measure_start` and stop at `measure_end` when one is given.

Exit categories: initial-stop, trailing-stop, strategy-exit, liquidation, end-of-data. There is no take-profit and no
risk-management exit in the simulator (the daily breaker halts entries, it does not close positions).

Assumptions (also written into the report metadata):
  * funding is a constant approximation unless real history is supplied (funding_mode="historical");
  * liquidation uses the isolated-margin formula with configurable brackets, on last-traded prices, and forfeits the whole
    margin. It is NOT an exact Binance liquidation simulation;
  * slippage is a flat per-tier assumption;
  * stops are tested on hourly OHLC: when a bar's range contains both a favourable and an adverse move, the order inside the
    bar is unknown, so a stop is assumed to trigger whenever the bar's low (high for shorts) reaches it;
  * the breaker sees hourly closes, not intrabar dips (live checks every minute, so it can trip earlier).
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd

from g_aegisbot.backtest.funding import FundingSchedule
from g_aegisbot.backtest.liquidation import LiquidationModel
from g_aegisbot.backtest.metrics import breakdown, compute_metrics
from g_aegisbot.config import DAILY_DRAWDOWN_LIMIT, DEFAULT_LEVERAGE
from g_aegisbot.costs import FUNDING_RATE_8H, MAKER_FEE, TAKER_FEE, slippage_rate
from g_aegisbot.engine import risk
from g_aegisbot.engine.strategies import candidate_mask, evaluate_symbol, exit_signal, validate_strategies
from g_aegisbot.engine.strategy import (
    PortfolioState, StrategyParams, TrailingStop, compute_indicators, compute_size, signal_rank_key,
)

MAINT_MARGIN = 0.005

EXIT_INITIAL_STOP = "initial-stop"
EXIT_TRAILING_STOP = "trailing-stop"
EXIT_STRATEGY = "strategy-exit"
EXIT_LIQUIDATION = "liquidation"
EXIT_END = "end-of-data"
EXIT_CATEGORIES = (EXIT_INITIAL_STOP, EXIT_TRAILING_STOP, EXIT_STRATEGY, EXIT_LIQUIDATION, EXIT_END)

FUNDING_MODES = ("approx", "historical", "none")


def build_assumptions(cfg: "BacktestConfig") -> dict:
    funding = {
        "approx": "SCENARIO 'approx': a constant approximation (default 0.01% per 8h charged on notional). Historical Binance "
                  "funding rates are NOT used.",
        "historical": "SCENARIO 'historical': real Binance funding rates, applied at each funding timestamp to positions open "
                      "then (longs pay a positive rate, shorts receive it), on notional at the price at that time. Mark price "
                      "is approximated by the candle open.",
        "none": "SCENARIO 'none': funding ignored entirely (isolates its effect; not a realistic result).",
    }[cfg.funding_mode]
    return {
        "funding": funding,
        "liquidation": f"Isolated-margin formula with maintenance-margin brackets ({cfg.liquidation.source}), last-traded prices, "
                       "whole margin forfeited on liquidation. This is NOT an exact Binance liquidation simulation: no mark "
                       "price, no cross-margin effects, bracket chosen by entry notional.",
        "slippage": "Flat per-tier assumption (3 / 6 / 12 basis points, stop orders 1.5x). Not measured fills.",
        "fills": "Signals use closed candles only; entries and strategy exits fill at the next bar's open; stops are tested "
                 "against bar high/low; a gap through the stop fills at the open. Order inside a bar is unknown (OHLC limit).",
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
    funding_mode: str = "approx"             # approx | historical | none
    funding_history: dict[str, FundingSchedule] | None = None
    funding_allow_gaps: bool = False         # historical mode: tolerate gaps (charged nothing, counted); off by default
    liquidation: LiquidationModel = field(default_factory=LiquidationModel)
    liquidation_overrides: dict[str, LiquidationModel] = field(default_factory=dict)   # symbol -> its own brackets
    slippage_scale: float = 1.0
    use_breaker: bool = True
    warmup_candles: int = 500                # closed candles a symbol needs before its first signal
    measure_start: "pd.Timestamp | str | None" = None   # first measured bar; default: bar number warmup_candles
    measure_end: "pd.Timestamp | str | None" = None     # exclusive end of the measured period; default: the last bar
    drawdown_limit: float = DAILY_DRAWDOWN_LIMIT
    strategies: tuple[str, ...] | None = None            # None: params.strategies
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
    strategy: str
    initial_stop: float
    liq_price: float
    funding: float = 0.0
    pending_exit: str | None = None

    @property
    def dir(self) -> int:
        return 1 if self.side == "buy" else -1

    def risk_at_stop(self) -> float:
        """Loss if the CURRENT stop is hit (0 once the stop has moved past entry)."""
        return max(0.0, (self.entry - self.trail.stop_price) * self.dir) * self.amount


@dataclass
class BacktestResult:
    trades: list[dict]
    equity: pd.Series
    metrics: dict
    by_symbol: dict
    notes: dict
    by_strategy: dict = field(default_factory=dict)
    by_exit: dict = field(default_factory=dict)


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class Backtester:
    def __init__(self, frames: dict[str, pd.DataFrame], cfg: BacktestConfig = BacktestConfig()):
        self.cfg, self.p = cfg, cfg.params
        self.strategies = validate_strategies(cfg.strategies if cfg.strategies is not None else cfg.params.strategies)
        if cfg.funding_mode not in FUNDING_MODES:
            raise ValueError(f"funding_mode must be one of {FUNDING_MODES}")
        self.tf = pd.Timedelta(seconds=ccxt.Exchange.parse_timeframe(self.p.timeframe))
        self.ind = {s: compute_indicators(df.sort_index(), self.p) for s, df in frames.items() if len(df)}
        if not self.ind:
            raise ValueError("no candle data supplied")
        self.cand = {s: candidate_mask(i, self.strategies) for s, i in self.ind.items()}
        self.arr = {s: {c: i[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close", "atr")}
                    for s, i in self.ind.items()}
        timeline = None
        for i in self.ind.values():
            timeline = i.index if timeline is None else timeline.union(i.index)

        ms = cfg.measure_start
        if ms is None:
            if len(timeline) <= cfg.warmup_candles:
                raise ValueError(f"need more than {cfg.warmup_candles} candles to leave a measured period, "
                                 f"got {len(timeline)}")
            ms = timeline[cfg.warmup_candles]
        self.measure_start = _ts(ms)
        if cfg.measure_end is not None:
            timeline = timeline[timeline < _ts(cfg.measure_end)]       # bars at or after measure_end are not replayed
        self.timeline = timeline
        if len(timeline) == 0 or self.measure_start > timeline[-1]:
            raise ValueError("measure_start is after the last candle in the measured range")
        self.pidx = {s: i.index.get_indexer(timeline) for s, i in self.ind.items()}
        self.last_time = {s: i.index[-1] for s, i in self.ind.items()}
        self._check_funding()

    # ---- funding availability: never silently replaced by made-up values
    def _check_funding(self) -> None:
        self.funding_gaps: dict[str, list[str]] = {}
        if self.cfg.funding_mode != "historical":
            return
        hist = self.cfg.funding_history or {}
        missing = [s for s in self.ind if s not in hist]
        if missing:
            raise ValueError(f"funding_mode='historical' but no funding history for {missing}. Download it, or choose "
                             "funding_mode='approx' to run the approximation as a separately labelled scenario.")
        end_ms = int((self.timeline[-1] + self.tf).timestamp() * 1000)
        for s in self.ind:
            first = max(self.ind[s].index[0], self.measure_start)
            probs = hist[s].problems(int(first.timestamp() * 1000), end_ms)
            if probs:
                self.funding_gaps[s] = probs
        if self.funding_gaps and not self.cfg.funding_allow_gaps:
            detail = "; ".join(f"{s}: {p[0]}" for s, p in self.funding_gaps.items())
            raise ValueError(f"historical funding data is incomplete ({detail}). Nothing was invented. Fix the data, or "
                             "set funding_allow_gaps to run with gaps charged nothing and reported.")

    # ---- cost model
    def _fill(self, symbol: str, price: float, buying: bool, *, is_stop: bool = False) -> float:
        slip = slippage_rate(symbol, is_stop=is_stop, scale=self.cfg.slippage_scale)
        return price * (1 + slip if buying else 1 - slip)

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
        sig_stats: dict[str, Counter] = {sid: Counter() for sid in self.strategies}
        decision_reasons: Counter = Counter()
        funding_events = funding_applied = 0
        funding_total = 0.0
        n = len(self.timeline)
        tf_ms = int(self.tf.total_seconds() * 1000)
        hist = cfg.funding_history or {}

        def close(pos: _Pos, exit_price: float, when: pd.Timestamp, reason: str, detail: str = "") -> None:
            nonlocal wallet
            if reason == EXIT_LIQUIDATION:                     # the whole isolated margin is forfeited
                gross, fee_out = -pos.margin, 0.0
                exit_price = pos.liq_price
            else:
                gross = (exit_price - pos.entry) * pos.amount * pos.dir
                fee_out = exit_price * pos.amount * cfg.taker_fee
            wallet += gross - fee_out
            trades.append({
                "symbol": pos.symbol, "side": pos.side, "strategy": pos.strategy, "conviction": pos.conviction,
                "entry_time": pos.opened_at, "exit_time": when, "entry_price": pos.entry, "exit_price": exit_price,
                "amount": pos.amount, "gross_pnl": gross, "pnl": gross - pos.fee_in - fee_out - pos.funding,
                "fees": pos.fee_in + fee_out, "funding": pos.funding, "reason": reason, "exit_detail": detail,
                "initial_stop": pos.initial_stop, "stop_at_exit": pos.trail.stop_price,
                "holding_hours": (when - pos.opened_at).total_seconds() / 3600,
            })
            del positions[pos.symbol]

        for k in range(n):
            t = self.timeline[k]
            t_close = t + self.tf
            measuring = t >= self.measure_start      # warm-up bars are replayed but never measured
            t_ms = int(t.timestamp() * 1000)

            # 0. a symbol whose history has ended is closed at its last close
            for s, pos in list(positions.items()):
                if t > self.last_time[s]:
                    close(pos, self._fill(s, last_close[s], pos.dir == -1), t, EXIT_END, "symbol data ended")

            # 1. historical funding settling inside this bar, on positions carried into it
            if cfg.funding_mode == "historical":
                for s, pos in positions.items():
                    i = self.pidx[s][k]
                    if i < 0:
                        continue
                    for _, rate in hist[s].events_in(t_ms, t_ms + tf_ms):
                        cost = pos.amount * self.arr[s]["open"][i] * rate * pos.dir
                        wallet -= cost
                        pos.funding += cost
                        funding_events += 1
                        funding_total += cost
                funding_applied = funding_events

            # 2. strategy exits decided at the previous close
            for s, pos in list(positions.items()):
                i = self.pidx[s][k]
                if pos.pending_exit and i >= 0:
                    close(pos, self._fill(s, self.arr[s]["open"][i], pos.dir == -1), t, EXIT_STRATEGY, pos.pending_exit)

            # 3. fill entries decided at the previous close
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
                mult = sig.trail_atr_mult or p.trail_atr_mult
                positions[s] = _Pos(
                    s, sig.side, plan.amount, entry, margin, fee, TrailingStop(sig.side, sig.stop_price, entry, mult), t,
                    sig.conviction, sig.strategy, sig.stop_price,
                    cfg.liquidation_overrides.get(s, cfg.liquidation).liquidation_price(sig.side, entry, plan.amount, cfg.leverage))
            pending = still_pending

            # 4. stops and liquidation
            for s, pos in list(positions.items()):
                i = self.pidx[s][k]
                if i < 0:
                    continue
                o, h, l = self.arr[s]["open"][i], self.arr[s]["high"][i], self.arr[s]["low"][i]
                stop, exit_price, liquidated = pos.trail.stop_price, None, False
                if pos.dir == 1 and l <= stop:
                    exit_price = self._fill(s, min(stop, o), False, is_stop=True)
                    liquidated = exit_price <= pos.liq_price
                elif pos.dir == -1 and h >= stop:
                    exit_price = self._fill(s, max(stop, o), True, is_stop=True)
                    liquidated = exit_price >= pos.liq_price
                if exit_price is not None:
                    if liquidated:
                        close(pos, exit_price, t_close, EXIT_LIQUIDATION, "price reached the liquidation level before the stop filled")
                    else:
                        cat = EXIT_INITIAL_STOP if pos.trail.stop_price == pos.initial_stop else EXIT_TRAILING_STOP
                        close(pos, exit_price, t_close, cat)

            # 5. approximate funding accrual, then trail the survivors (new stop applies from the next bar)
            for s, pos in positions.items():
                i = self.pidx[s][k]
                if i < 0:
                    continue
                if cfg.funding_mode == "approx":
                    cost = pos.amount * self.arr[s]["close"][i] * cfg.funding_rate_8h * (self.tf.total_seconds() / 28800) * pos.dir
                    wallet -= cost
                    pos.funding += cost
                    funding_total += cost
                pos.trail.update(self.arr[s]["high"][i], self.arr[s]["low"][i], self.arr[s]["atr"][i])

            # 6. mark equity, run the breaker
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

            # 7. on this bar's close: strategy exits for open positions, then new signals
            if not measuring or k == n - 1:
                continue
            for s, pos in positions.items():
                i = self.pidx[s][k]
                if i >= 0 and pos.pending_exit is None:
                    pos.pending_exit = exit_signal(pos.strategy, self.ind[s], int(i), pos.side)
            if tripped:
                continue
            results = []
            for s in self.ind:
                i = self.pidx[s][k]
                if (i >= cfg.warmup_candles and self.cand[s][i] and s not in positions
                        and all(s != q[0].symbol for q in pending)):   # i >= 500 also excludes i == -1 (no bar)
                    result, decisions = evaluate_symbol(s, self.ind[s], int(i), p, self.strategies)
                    results.append(result)
                    for d in decisions:
                        sig_stats[d.strategy]["generated"] += 1
                        sig_stats[d.strategy]["kept" if d.kept else "dropped"] += 1
                        if not d.kept:
                            decision_reasons[d.reason.split(" (")[0].split(" as ")[0]] += 1
                    if not decisions and result.signal:
                        sig_stats[result.signal.strategy]["generated"] += 1
                        sig_stats[result.signal.strategy]["kept"] += 1
            signals = sorted((r.signal for r in results if r.signal), key=lambda x: signal_rank_key(x, p.strategy_priority))
            for sig in signals:
                cap = cfg.wallet_cap if cfg.wallet_cap is not None else float("inf")
                used = sum(q.margin for q in positions.values()) + sum(pl.margin for _, pl in pending)
                longs = sum(q.dir == 1 for q in positions.values()) + sum(sg.side == "buy" for sg, _ in pending)
                shorts = len(positions) + len(pending) - longs
                state = PortfolioState(
                    open_risk=sum(q.risk_at_stop() for q in positions.values()) + sum(pl.risk_usdt for _, pl in pending),
                    open_notional=sum(q.amount * q.entry for q in positions.values()) + sum(pl.notional for _, pl in pending),
                    longs=longs, shorts=shorts)
                plan = compute_size(
                    sig, leverage=cfg.leverage, allocated=min(cap, equity), used=used, free=equity - used,
                    open_positions=len(positions) + len(pending), p=p, state=state)
                if hasattr(plan, "reason"):
                    skips[" ".join(plan.reason.split()[:3])] += 1
                    continue
                pending.append((sig, plan))

        # close whatever is still open at the last close
        for s, pos in list(positions.items()):
            close(pos, self._fill(s, last_close[s], pos.dir == -1), self.timeline[-1] + self.tf, EXIT_END, "end of measured data")
        eq_v[-1] = wallet
        equity_s = pd.Series(eq_v, index=pd.DatetimeIndex(eq_t)).groupby(level=0).last()

        start, end = equity_s.index[0], equity_s.index[-1]
        metrics = compute_metrics(equity_s, trades, cfg.initial_capital, start, end)
        by_symbol = {s: v for s, v in breakdown(trades, "symbol").items()}
        for s in self.ind:
            by_symbol.setdefault(s, {"trades": 0, "net_pnl": 0.0, "win_rate": 0.0})
        by_strategy = breakdown(trades, "strategy")
        by_exit = breakdown(trades, "reason")
        first_eligible = {}
        for s, i in self.ind.items():
            first_eligible[s] = str(max(i.index[cfg.warmup_candles], self.measure_start)) if len(i) > cfg.warmup_candles else None
        assumptions = build_assumptions(cfg)
        notes = {
            "measurement_period": {"start": str(self.measure_start), "end": str(self.timeline[-1] + self.tf)},
            "warmup": {"required_candles": cfg.warmup_candles, "excluded_from_statistics": True,
                       "first_signal_eligible": first_eligible},
            "data_range_including_warmup": {s: [str(i.index[0]), str(i.index[-1])] for s, i in self.ind.items()},
            "strategies": list(self.strategies),
            "signals": {sid: dict(c) for sid, c in sig_stats.items()},
            "signals_dropped_reasons": dict(decision_reasons),
            "breaker_trip_days": sorted(tripped_days), "skipped_signals": dict(skips),
            "funding": {"mode": cfg.funding_mode, "events_applied": funding_applied if cfg.funding_mode == "historical" else None,
                        "net_paid": funding_total, "gaps": self.funding_gaps if cfg.funding_mode == "historical" else {}},
            "exit_categories": list(EXIT_CATEGORIES),
            "assumptions": {"taker_fee": cfg.taker_fee, "funding_rate_8h": cfg.funding_rate_8h,
                            "funding_mode": cfg.funding_mode, "slippage_scale": cfg.slippage_scale, "leverage": cfg.leverage,
                            "maintenance_margin": cfg.liquidation.brackets[0].mmr, **assumptions},
        }
        return BacktestResult(trades, equity_s, metrics, by_symbol, notes, by_strategy, by_exit)
