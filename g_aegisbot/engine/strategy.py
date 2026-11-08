"""Breakout strategy engine: indicators, signal rules, ATR trailing stop, sizing, pre-trade checks.

Rules (all evaluated on the last CLOSED candle):
  1. Breakout: close beyond the prior N-candle high (long) or low (short).
  2. Trend: long needs close > EMA50 > EMA200; short needs close < EMA50 < EMA200.
  3. RSI band: long 50-70, short 30-50 (momentum without being stretched).
  4. Volume: candle volume >= 1.5 x the 20-candle volume average (prior 20 candles).
  5. Stop: entry -/+ 2 x ATR, trailed with a ratchet that only moves in the trade's favour.

This module also holds what every strategy shares: the indicator frame, sizing and portfolio limits (`compute_size`), the
trailing stop, and the one ranking of competing signals (`signal_rank_key`: strict strategy priority first, then conviction,
then volume ratio). The second strategy (trend pullback) lives in engine/strategies.py and engine/trend.py.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Literal

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd

from g_aegisbot.engine.protection import assess_stops
from g_aegisbot.exchange.binance_futures import parse_position_qty
from g_aegisbot.engine.indicators import atr, ema, rsi  # noqa: F401  (re-exported)
from g_aegisbot.engine.trend import add_trend_columns
from g_aegisbot.config import (
    DEFAULT_COINS, DEFAULT_LEVERAGE, MAX_LEVERAGE, MAX_OPEN_POSITIONS, MAX_RISK_PER_TRADE, MIN_LEVERAGE,
)

log = logging.getLogger(__name__)

ELITE_SYMBOLS: tuple[str, ...] = DEFAULT_COINS
Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class StrategyParams:
    timeframe: str = "1h"
    candles: int = 500            # EMA200 needs a long warm-up to converge
    min_candles: int = 250
    breakout_lookback: int = 20
    ema_fast: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    rsi_long: tuple[float, float] = (50.0, 70.0)
    rsi_short: tuple[float, float] = (30.0, 50.0)
    vol_period: int = 20
    vol_mult: float = 1.5         # hard gate
    high_vol_mult: float = 2.5    # conviction criterion
    atr_period: int = 14
    stop_atr_mult: float = 2.0
    trail_atr_mult: float = 2.0
    max_stop_pct: float = 0.15    # reject setups whose stop is wider than this (liquidation at 3x is ~33%)
    normal_pct: tuple[float, float] = (0.05, 0.07)   # share of allocated capital used as margin
    high_pct: tuple[float, float] = (0.12, 0.15)
    leverage: int = DEFAULT_LEVERAGE                 # fixed
    margin_buffer: float = 1.05   # free margin must cover margin + 5% for fees/slippage
    scan_concurrency: int = 5
    max_open_positions: int = MAX_OPEN_POSITIONS
    # --- portfolio risk (apply to every strategy combined)
    max_aggregate_risk_pct: float = 0.04    # sum of open risk at current stops, as a share of allocated capital (0 disables)
    max_same_direction: int = 3             # most positions allowed on one side at once (0 disables); alts are highly correlated
    max_gross_notional_x: float = 2.0       # total open notional as a multiple of allocated capital (0 disables)
    # --- strategy B: trend-following pullback (see docs/STRATEGIES.md). Fixed a priori, never tuned on results.
    pull_ema: int = 20
    trend_slope_bars: int = 24
    pullback_window: int = 8
    swing_lookback: int = 20
    min_pullback_atr: float = 1.0
    trend_rsi_mid: float = 50.0
    trend_vol_mult: float = 1.0
    trend_stop_atr_mult: float = 2.0
    trend_trail_atr_mult: float = 3.0
    # --- which strategies run, and which wins when two signal the same symbol in the same direction
    strategies: tuple[str, ...] = ("breakout",)
    strategy_priority: tuple[str, ...] = ("breakout", "trend")
    max_risk_pct: float = MAX_RISK_PER_TRADE   # of allocated capital, at the initial stop


# --------------------------------------------------------------------------- indicators
# ema, rsi and atr live in engine/indicators.py (re-exported here so existing imports keep working)


# --------------------------------------------------------------------------- signals
@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    candle_time: pd.Timestamp
    entry_price: float
    atr: float
    stop_price: float
    volume_ratio: float
    rsi: float
    conviction: Literal["normal", "high"]
    size_pct: float
    strategy: str = "breakout"
    trail_atr_mult: float | None = None     # None: use StrategyParams.trail_atr_mult


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    signal: Signal | None
    reason: str  # why it was rejected, or "signal"


def _reject(symbol: str, reason: str) -> ScanResult:
    return ScanResult(symbol, None, reason)


def _lerp(rng: tuple[float, float], fraction: float) -> float:
    return rng[0] + (rng[1] - rng[0]) * max(0.0, min(1.0, fraction))


def compute_indicators(df: pd.DataFrame, p: StrategyParams = StrategyParams()) -> pd.DataFrame:
    """All indicators for a candle frame. Every column is causal (uses only past and current
    candles), so the same frame serves live trading and the backtester without look-ahead.
    Levels and the volume average use the PRIOR candles only, so the signal candle cannot
    lift its own benchmark."""
    out = df[["open", "high", "low", "close", "volume"]].copy()
    out["ema_f"] = ema(out["close"], p.ema_fast)
    out["ema_s"] = ema(out["close"], p.ema_slow)
    out["rsi"] = rsi(out["close"], p.rsi_period)
    out["atr"] = atr(out, p.atr_period)
    out["level_high"] = out["high"].shift(1).rolling(p.breakout_lookback).max()
    out["level_low"] = out["low"].shift(1).rolling(p.breakout_lookback).min()
    out["vol_ma"] = out["volume"].shift(1).rolling(p.vol_period).mean()
    add_trend_columns(out, p)
    return out


def breakout_candidates(ind: pd.DataFrame) -> np.ndarray:
    """Boolean array: bars that closed beyond the breakout level (cheap pre-filter for backtests)."""
    return ((ind["close"] > ind["level_high"]) | (ind["close"] < ind["level_low"])).to_numpy()


def evaluate_at(symbol: str, ind: pd.DataFrame, i: int, p: StrategyParams = StrategyParams()) -> ScanResult:
    """Apply the entry rules to bar number i of an indicator frame."""
    if i + 1 < p.min_candles:
        return _reject(symbol, f"not enough candles ({i + 1}<{p.min_candles})")
    row = ind.iloc[i]
    c, a, r, vma = row["close"], row["atr"], row["rsi"], row["vol_ma"]
    ef, es, lh, ll = row["ema_f"], row["ema_s"], row["level_high"], row["level_low"]
    if any(pd.isna(v) for v in (c, a, r, vma, ef, es, lh, ll)) or a <= 0 or vma <= 0:
        return _reject(symbol, "indicators not ready")

    if c > lh:
        side: Side = "buy"
        level = lh
    elif c < ll:
        side = "sell"
        level = ll
    else:
        return _reject(symbol, "no breakout")
    long = side == "buy"

    if long and not (c > ef > es):
        return _reject(symbol, "trend filter (long needs close>EMA50>EMA200)")
    if not long and not (c < ef < es):
        return _reject(symbol, "trend filter (short needs close<EMA50<EMA200)")

    lo, hi = p.rsi_long if long else p.rsi_short
    if not lo <= r <= hi:
        return _reject(symbol, f"RSI filter ({r:.1f} outside {lo}-{hi})")

    vol_ratio = float(row["volume"] / vma)
    if vol_ratio < p.vol_mult:
        return _reject(symbol, f"volume filter ({vol_ratio:.2f}x < {p.vol_mult}x)")

    stop_dist = p.stop_atr_mult * a
    if stop_dist / c > p.max_stop_pct:
        return _reject(symbol, f"stop too wide ({stop_dist / c:.1%})")
    stop = c - stop_dist if long else c + stop_dist

    # Conviction: 4 criteria, "high" needs at least 3.
    breakout_atr = abs(c - level) / a
    ema_slope = (ef - ind["ema_f"].iloc[i - 5]) * (1 if long else -1)
    sweet = (55 <= r <= 65) if long else (35 <= r <= 45)
    hits = sum([
        vol_ratio >= p.high_vol_mult,
        breakout_atr >= 0.5,
        ema_slope > 0 and abs(ef - es) / c >= 0.02,
        sweet,
    ])
    if hits >= 3:
        conviction, pct = "high", _lerp(p.high_pct, hits - 3)
    else:
        conviction, pct = "normal", _lerp(p.normal_pct, hits / 2)

    sig = Signal(symbol, side, ind.index[i], float(c), float(a), float(stop), vol_ratio, float(r), conviction, pct)
    return ScanResult(symbol, sig, "signal")


def evaluate(symbol: str, df: pd.DataFrame, p: StrategyParams = StrategyParams()) -> ScanResult:
    """Evaluate the last row of CLOSED candles (index = time; columns open/high/low/close/volume)."""
    if len(df) < p.min_candles:
        return _reject(symbol, f"not enough candles ({len(df)}<{p.min_candles})")
    return evaluate_at(symbol, compute_indicators(df, p), len(df) - 1, p)


# --------------------------------------------------------------------------- trailing stop
@dataclass
class TrailingStop:
    """Ratchet stop: moves only in the trade's favour, never back."""
    side: Side
    stop_price: float
    extreme_price: float          # highest high (long) / lowest low (short) since entry
    atr_mult: float = 2.0

    @classmethod
    def from_signal(cls, sig: Signal, atr_mult: float = 2.0) -> "TrailingStop":
        return cls(sig.side, sig.stop_price, sig.entry_price, atr_mult)

    def update(self, candle_high: float, candle_low: float, current_atr: float) -> bool:
        """Feed each new closed candle. Returns True if the stop moved (re-place the exchange stop)."""
        if self.side == "buy":
            self.extreme_price = max(self.extreme_price, candle_high)
            candidate = self.extreme_price - self.atr_mult * current_atr
            if candidate > self.stop_price:
                self.stop_price = candidate
                return True
        else:
            self.extreme_price = min(self.extreme_price, candle_low)
            candidate = self.extreme_price + self.atr_mult * current_atr
            if candidate < self.stop_price:
                self.stop_price = candidate
                return True
        return False


# --------------------------------------------------------------------------- sizing + pre-trade checks
@dataclass(frozen=True)
class TradePlan:
    symbol: str
    side: Side
    amount: float
    entry_price: float
    stop_price: float
    leverage: int
    margin: float
    notional: float
    risk_usdt: float              # loss if the initial stop is hit (before fees/slippage)
    conviction: str
    size_pct: float
    risk_capped: bool = False     # True when the size was cut to respect the per-trade risk cap
    strategy: str = "breakout"
    trail_atr_mult: float | None = None
    risk_note: str = ""          # which portfolio limit trimmed the size, if any


@dataclass(frozen=True)
class Skip:
    symbol: str
    reason: str


@dataclass(frozen=True)
class PortfolioState:
    """What the account already holds, across ALL strategies. Computed from actual positions, not from signals."""
    open_risk: float = 0.0        # sum over open positions of the loss if each hits its CURRENT stop (0 once a stop is past entry)
    open_notional: float = 0.0
    longs: int = 0
    shorts: int = 0


def portfolio_state(positions: list[dict], trades=(), unprotected_loss_pct: float = 0.03) -> PortfolioState:
    """Build the state from exchange positions (symbol, side 'long'/'short', contracts, entryPrice) and the stop
    records of managed trades. A position with no known stop is counted at a conservative loss, never at zero."""
    stops = {t.symbol: float(t.stop_price) for t in trades if getattr(t, "stop_price", None)}
    risk = notional = 0.0
    longs = shorts = 0
    for pos in positions:
        amount, entry = float(pos.get("contracts") or 0), float(pos.get("entryPrice") or 0)
        side = pos.get("side")
        is_long = side == "long"
        # a position of unknown side counts against both limits: conservative, never silently ignored
        longs += side in ("long", None) or side not in ("long", "short")
        shorts += side in ("short", None) or side not in ("long", "short")
        notional += amount * entry
        stop = stops.get(pos.get("symbol"))
        if stop is not None and entry > 0 and side in ("long", "short"):
            per_unit = max(0.0, (entry - stop) if is_long else (stop - entry))
        else:
            per_unit = entry * unprotected_loss_pct
        risk += per_unit * amount
    return PortfolioState(risk, notional, longs, shorts)


def compute_size(
    sig: Signal, *, leverage: int, allocated: float, used: float, free: float, open_positions: int,
    p: StrategyParams = StrategyParams(), round_amount=None, min_amount: float | None = None,
    min_cost: float | None = None, state: PortfolioState | None = None,
) -> TradePlan | Skip:
    """Pure sizing and risk rules, shared by live trading, the paper ledger and the backtester.
    `state` carries the combined portfolio (all strategies). Without it only the per-trade rules apply."""
    sym = sig.symbol
    if open_positions >= p.max_open_positions:
        return Skip(sym, f"max {p.max_open_positions} open positions reached")
    if state is not None and p.max_same_direction:
        same = state.longs if sig.side == "buy" else state.shorts
        if same >= p.max_same_direction:
            return Skip(sym, f"max {p.max_same_direction} positions already open on the "
                             f"{'long' if sig.side == 'buy' else 'short'} side (correlated)")
    if not MIN_LEVERAGE <= leverage <= MAX_LEVERAGE:
        return Skip(sym, f"leverage {leverage}x outside {MIN_LEVERAGE}-{MAX_LEVERAGE}x")
    stop_dist = abs(sig.entry_price - sig.stop_price)
    if stop_dist / sig.entry_price > 0.5 / leverage:  # keep the stop well inside the liquidation distance
        return Skip(sym, f"stop {stop_dist / sig.entry_price:.1%} too close to liquidation at {leverage}x")

    margin = allocated * sig.size_pct
    headroom = allocated - used
    if margin > headroom:
        return Skip(sym, f"allocation headroom {headroom:.2f} < margin {margin:.2f}")
    if free < margin * p.margin_buffer:
        return Skip(sym, f"insufficient free margin ({free:.2f} < {margin * p.margin_buffer:.2f})")

    amount = margin * leverage / sig.entry_price
    risk_note = ""
    max_amount = allocated * p.max_risk_pct / stop_dist      # cap loss at the stop to max_risk_pct
    if amount > max_amount:
        amount, risk_note = max_amount, "per-trade risk limit"
    if state is not None:
        if p.max_aggregate_risk_pct:
            budget = allocated * p.max_aggregate_risk_pct - state.open_risk
            if budget <= 0:
                return Skip(sym, f"aggregate open risk {state.open_risk:.2f} already at the "
                                 f"{p.max_aggregate_risk_pct:.0%} limit ({allocated * p.max_aggregate_risk_pct:.2f})")
            if amount > budget / stop_dist:
                amount, risk_note = budget / stop_dist, "aggregate open-risk limit"
        if p.max_gross_notional_x:
            room = allocated * p.max_gross_notional_x - state.open_notional
            if room <= 0:
                return Skip(sym, f"gross notional {state.open_notional:.2f} already at {p.max_gross_notional_x:g}x allocated capital")
            if amount * sig.entry_price > room:
                amount, risk_note = room / sig.entry_price, "gross notional limit"
    risk_capped = bool(risk_note)
    if round_amount:
        amount = round_amount(amount)                         # exchange rounding truncates (never up)
    if amount <= 0:
        return Skip(sym, "size rounds to zero")
    if min_amount and amount < min_amount:
        return Skip(sym, f"below min amount {min_amount}")
    if min_cost and amount * sig.entry_price < min_cost:
        return Skip(sym, f"below min notional {min_cost}")

    notional = amount * sig.entry_price
    return TradePlan(sym, sig.side, amount, sig.entry_price, sig.stop_price, leverage, notional / leverage,
                     notional, amount * stop_dist, sig.conviction, sig.size_pct, risk_capped,
                     strategy=sig.strategy, trail_atr_mult=sig.trail_atr_mult, risk_note=risk_note)


async def plan_trade(profile, adapter, sig: Signal, p: StrategyParams = StrategyParams(), trades=()) -> TradePlan | Skip:
    """Verify the account can take the trade and size it. Never raises on 'not enough money':
    it returns a Skip so the scan loop moves on to the next pair."""
    sym = sig.symbol
    if not profile.subscription_is_active():
        return Skip(sym, "subscription inactive")
    if profile.daily_loss_tracker.get("breaker_tripped"):
        return Skip(sym, "daily drawdown breaker tripped")
    cap = float(profile.max_wallet_cap_usdt or 0)
    if cap <= 0:
        return Skip(sym, "max_wallet_cap_usdt is 0 (trading blocked)")

    positions = await adapter.fetch_open_positions()
    if any(pos.get("symbol") == sym for pos in positions):
        return Skip(sym, "position already open")

    bal = await adapter.fetch_usdt_balance()
    ex = adapter.exchange
    market = (ex.markets or {}).get(sym) or {}
    limits = market.get("limits", {})
    return compute_size(
        sig,
        leverage=int(getattr(profile, "leverage", None) or p.leverage),
        allocated=min(cap, bal["total"]),
        used=bal["used"],
        free=bal["free"],
        open_positions=len(positions),
        state=portfolio_state(positions, trades),
        p=p,
        round_amount=lambda a: float(ex.amount_to_precision(sym, a)),
        min_amount=(limits.get("amount") or {}).get("min"),
        min_cost=(limits.get("cost") or {}).get("min"),
    )


class OrderUncertain(Exception):
    """An order was sent but its outcome could not be established. The caller must NOT resend it. It must block new
    entries, reconcile by the client order id, and re-read positions and outstanding orders first."""

    def __init__(self, symbol: str, client_id: str, cause: Exception | str | None = None):
        self.symbol, self.client_id, self.cause = symbol, client_id, cause
        detail = f" ({type(cause).__name__})" if isinstance(cause, Exception) else (f" ({cause})" if cause else "")
        super().__init__(f"{symbol}: order {client_id} may or may not have been placed{detail}")


class OrderNotFilled(Exception):
    """The entry did not open a position.

    definitive=True : the exchange confirmed it (rejected, or a terminal canceled/expired status with zero fills).
    definitive=False: the exchange has no record of the client id, but the request may still be in flight after a
                      timeout, so the caller must keep entries blocked and reconcile before trusting it."""

    def __init__(self, message: str, *, definitive: bool = True, symbol: str | None = None, client_id: str | None = None,
                 rejected: bool = False):
        super().__init__(message)
        # `definitive` records what the exchange said at that moment. The runner does NOT release on it: every failed entry
        # goes through the central reconciliation policy (engine/reconcile.py). `rejected` marks an explicit rejection of
        # the create request, which that policy accepts only together with clean evidence from every source.
        self.definitive, self.symbol, self.client_id, self.rejected = definitive, symbol, client_id, rejected


class ProtectionFailure(Exception):
    """A position exists (or may exist) that could not be protected or confirmed closed. Entries must stop."""

    def __init__(self, symbol: str, client_id: str | None, detail: str):
        self.symbol, self.client_id, self.detail = symbol, client_id, detail
        super().__init__(f"{symbol}: {detail}")


TERMINAL_UNFILLED = ("canceled", "cancelled", "expired", "rejected")


def classify_entry(order: dict | None) -> tuple[str, float | None]:
    """What an order status proves. Returns (kind, filled quantity as reported, or None if not reported).

      unfilled       a terminal canceled/expired/rejected status AND zero cumulative fill: no exposure, provably
      partial_closed a terminal status with a positive fill: the filled part is a live position, the rest is gone
      filled         status closed with a positive fill
      pending        anything else (open, new, partially filled and still working, missing or unknown status, a closed
                     order that reports no fill). Never treated as unfilled: it may still fill."""
    if not order:
        return "pending", None
    status = str(order.get("status") or "").lower()
    raw = order.get("filled")
    try:
        filled = None if raw is None else float(raw)
    except (TypeError, ValueError):
        filled = None
    if status in TERMINAL_UNFILLED:
        if filled is None:
            return "pending", None                       # terminal, but the fill is unknown: cannot call it unfilled
        return ("unfilled", 0.0) if filled <= 0 else ("partial_closed", filled)
    if status == "closed" and filled is not None and filled > 0:
        return "filled", filled
    return "pending", filled


def new_client_order_id(plan: TradePlan, now: datetime | None = None) -> str:
    """Unique, exchange-legal (<=36 chars, [A-Za-z0-9_-]) id. Re-using it can never create a second order."""
    import secrets
    stamp = int((now or datetime.now(timezone.utc)).timestamp())
    base = plan.symbol.split("/")[0].replace("1000", "k")
    return f"aeg-{plan.strategy[:3]}-{base[:8]}-{stamp}-{secrets.token_hex(4)}"      # <= 36 chars; 32 random bits


async def _lookup(adapter, plan: TradePlan, client_id: str, sleep, attempts: int = 3):
    """Ask the exchange about THIS client id. Returns the order, or None when the exchange answered that it has no such
    order. Raises OrderUncertain when it cannot be reached or cannot look orders up."""
    lookup = getattr(adapter, "fetch_order_by_client_id", None)
    if lookup is None:
        raise OrderUncertain(plan.symbol, client_id, "this adapter cannot look orders up by client id")
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await lookup(plan.symbol, client_id)
        except Exception as exc:                            # still cannot reach the exchange
            last = exc
            log.warning("%s: lookup of order %s failed (%s), attempt %d/%d", plan.symbol, client_id,
                        type(exc).__name__, attempt + 1, attempts)
            await sleep(2 ** attempt)
    raise OrderUncertain(plan.symbol, client_id, last)


async def _position_qty(adapter, symbol: str) -> float | None:
    """Quantity of the open position on the exchange (0.0 when flat), or None when it could not be read."""
    reader = getattr(adapter, "fetch_open_positions", None)
    if reader is None:
        return None
    try:
        total, seen = 0.0, False
        for pos in await reader([symbol]):
            size = parse_position_qty(pos)                 # a malformed entry raises: unreadable, never flat
            if pos.get("symbol") == symbol:
                total, seen = total + size, True
        return total if seen else 0.0
    except Exception as exc:
        log.warning("%s: could not read the exchange position (%s)", symbol, type(exc).__name__)
        return None


async def _settle_entry(adapter, plan: TradePlan, client_id: str, order: dict | None, sleep, polls: int = 3) -> tuple[dict, bool]:
    """Drive an entry order to a terminal state using its client id. Returns (order, outstanding).

    `outstanding` is True when some quantity has filled but the order is still working after a cancel attempt. The
    filled part is then protected by the caller and the remainder is reconciled by the runner."""
    if order is None:                                       # a timeout: ask whether the request ever arrived
        order = await _lookup(adapter, plan, client_id, sleep)
        if order is None:
            raise OrderNotFilled(f"{plan.symbol}: the exchange has no order {client_id}, but the request may still be "
                                 "in flight after the timeout", definitive=False, symbol=plan.symbol, client_id=client_id)
    for attempt in range(polls + 1):
        kind, filled = classify_entry(order)
        if kind == "unfilled":
            raise OrderNotFilled(f"{plan.symbol}: entry {client_id} ended {order.get('status')} with nothing filled",
                                 definitive=True, symbol=plan.symbol, client_id=client_id)
        if kind in ("filled", "partial_closed"):
            return order, False
        if attempt == polls:
            break
        await sleep(0.5 * 2 ** attempt)
        fresh = await _lookup(adapter, plan, client_id, sleep)
        if fresh is None:
            raise OrderUncertain(plan.symbol, client_id, "the exchange no longer returns an order it accepted")
        order = fresh
    # still working: try to cancel what is left, then look once more
    canceller = getattr(adapter, "cancel_order_by_client_id", None)
    if canceller is not None:
        try:
            await canceller(plan.symbol, client_id)
        except Exception as exc:
            log.warning("%s: could not cancel the remainder of %s (%s)", plan.symbol, client_id, type(exc).__name__)
        fresh = await _lookup(adapter, plan, client_id, sleep)
        if fresh is not None:
            order = fresh
        kind, filled = classify_entry(order)
        if kind == "unfilled":
            raise OrderNotFilled(f"{plan.symbol}: entry {client_id} ended {order.get('status')} with nothing filled",
                                 definitive=True, symbol=plan.symbol, client_id=client_id)
        if kind in ("filled", "partial_closed"):
            return order, False
    kind, filled = classify_entry(order)
    if filled and filled > 0:
        return order, True                                  # part filled and still working: protect that part now
    raise OrderUncertain(plan.symbol, client_id, f"status {order.get('status')!r}, fill {filled}")


async def _confirm_stop_visible(adapter, plan: TradePlan, client_id: str, sleep, attempts: int = 3) -> None:
    """A stop that was merely ACCEPTED is not protection. Re-read the exchange: a valid stop (right side, reduce-only, whole
    quantity) must be listed against the position as it now stands. Raises ProtectionFailure otherwise."""
    fetch = getattr(adapter, "fetch_stop_orders", None)
    side = "long" if plan.side == "buy" else "short"
    last = "the exchange could not be re-read"
    for attempt in range(attempts):
        if fetch is None:
            last = "this adapter cannot list stop orders"
            break
        try:
            qty = await _position_qty(adapter, plan.symbol)
            stops = await fetch(plan.symbol)
        except Exception as exc:
            last = f"the exchange could not be re-read ({type(exc).__name__})"
        else:
            if qty is None or qty <= 0:
                last = "the position could not be re-read"
            elif assess_stops(stops, position_side=side, position_qty=qty).best is not None:
                return
            else:
                last = "a valid full-size stop is not visible on the exchange"
        if attempt + 1 < attempts:
            await sleep(0.5 * 2 ** attempt)
    raise ProtectionFailure(plan.symbol, client_id, f"the stop was accepted but protection is not confirmed: {last}")


async def execute_plan(adapter, plan: TradePlan, *, sleep=asyncio.sleep, before_send=None) -> dict:
    """Open the position with an idempotent entry, then place the exchange-side stop for what was ACTUALLY filled.

    Returns when the position is open and its stop was accepted. Raises:
      OrderNotFilled   (definitive) the entry provably did not open a position; (not definitive) not seen yet, keep
                       entries blocked and reconcile;
      OrderUncertain   the outcome cannot be established; never resent;
      ProtectionFailure a position exists or may exist and could not be protected or confirmed closed;
      the original error when the stop failed but the emergency close was confirmed by the exchange position."""
    await adapter.configure_symbol(plan.symbol, plan.leverage)
    client_id = new_client_order_id(plan)
    if before_send is not None:
        await before_send(client_id)             # durable record of the intent BEFORE anything can reach the exchange;
                                                 # if it cannot be written, nothing is sent
    order: dict | None = None
    try:
        order = await adapter.market_order(plan.symbol, plan.side, plan.amount, client_id=client_id, strategy=plan.strategy)
    except ccxt.NetworkError:                               # timeout, rate limit, unavailable: outcome unknown, order is None
        pass
    except ccxt.ExchangeError as exc:                       # the exchange answered with an error for this request
        raise OrderNotFilled(f"{plan.symbol}: entry rejected ({type(exc).__name__})", definitive=True,
                             symbol=plan.symbol, client_id=client_id, rejected=True) from exc
    except Exception as exc:                                # unknown failure: it may have reached the exchange
        raise OrderUncertain(plan.symbol, client_id, exc) from exc
    order, outstanding = await _settle_entry(adapter, plan, client_id, order, sleep)

    _, reported = classify_entry(order)
    on_exchange = await _position_qty(adapter, plan.symbol)
    if on_exchange is not None and on_exchange > 0:
        filled = on_exchange                                # the exchange position is the authority on what to protect
    elif on_exchange == 0.0:
        raise OrderUncertain(plan.symbol, client_id, f"the order reports a fill of {reported} but the exchange shows no position")
    elif reported and reported > 0:
        filled = reported                                   # position unreadable: protect what the order says filled
    else:
        raise OrderUncertain(plan.symbol, client_id, "fill quantity unknown and the position could not be read")
    if filled < plan.amount * 0.999:
        log.warning("%s: PARTIAL FILL %.8g of %.8g; the stop covers the filled quantity only", plan.symbol, filled, plan.amount)
    exit_side = "sell" if plan.side == "buy" else "buy"
    try:
        stop = await adapter.stop_market(plan.symbol, exit_side, filled, plan.stop_price)
    except Exception as stop_exc:
        log.exception("Stop placement failed for %s; closing position", plan.symbol)
        close_error = None
        try:
            await adapter.market_order(plan.symbol, exit_side, filled, reduce_only=True)
        except Exception as exc:
            close_error = exc
        remaining = await _position_qty(adapter, plan.symbol)
        if remaining == 0.0:
            raise stop_exc                                  # the exchange confirms there is no position left
        state = "unknown (could not be read)" if remaining is None else f"{remaining:g} still open"
        closing = f"emergency close failed ({type(close_error).__name__})" if close_error else "emergency close not confirmed"
        raise ProtectionFailure(plan.symbol, client_id,
                                f"stop placement failed ({type(stop_exc).__name__}); {closing}; position {state}") from stop_exc
    await _confirm_stop_visible(adapter, plan, client_id, sleep)
    return {"entry": order, "stop": stop, "stop_order_id": (stop or {}).get("id"), "filled": filled,
            "client_order_id": client_id, "partial": filled < plan.amount * 0.999, "outstanding": outstanding}


def rank_signals(results: list[ScanResult], priority: tuple[str, ...]) -> list[Signal]:
    """Signals only, best first, in the order of `signal_rank_key`. The priority is required, never defaulted, so no
    caller can rank without saying which strategy comes first."""
    sigs = [r.signal for r in results if r.signal]
    return sorted(sigs, key=lambda s: signal_rank_key(s, priority))


def signal_rank_key(s: Signal, priority: tuple[str, ...]) -> tuple:
    """THE ranking of competing signals, used by the per-symbol choice, the live scanner, the live runner and the
    backtester. Strict strategy priority comes first: every signal of a strategy listed earlier in `priority` outranks
    every signal of a later one, whatever its conviction or volume. Within one strategy, high conviction comes first,
    then the larger volume ratio. A strategy missing from `priority` ranks after all listed ones, and ties end on symbol,
    side and strategy name, so two runs over the same data always pick the same signals in the same sequence."""
    rank = priority.index(s.strategy) if s.strategy in priority else len(priority)
    return (rank, s.conviction != "high", -round(s.volume_ratio, 9), s.symbol, s.side, s.strategy)


# --------------------------------------------------------------------------- scanner
@dataclass
class MultiPairScanner:
    adapter: object
    symbols: tuple[str, ...] | list[str] = ELITE_SYMBOLS
    params: StrategyParams = field(default_factory=StrategyParams)
    indicators: dict = field(default_factory=dict)   # symbol -> indicator frame of CLOSED candles, refreshed each scan
    decisions: list = field(default_factory=list)    # SignalDecision records of the latest scan (kept, duplicate, conflict)
    clock: object = lambda: datetime.now(timezone.utc)   # "now"; a candle is closed only once its time has passed

    async def prepare(self) -> list[str]:
        """Load markets and drop symbols Binance does not list. Returns the dropped symbols."""
        await self.adapter.exchange.load_markets()
        bad = await self.adapter.validate_symbols(list(self.symbols))
        if bad:
            log.warning("Dropping unknown/untradable symbols: %s", bad)
            self.symbols = [s for s in self.symbols if s not in bad]
        return bad

    async def _scan_one(self, sem: asyncio.Semaphore, symbol: str) -> ScanResult:
        try:
            async with sem:
                df = await self.adapter.fetch_ohlcv_df(symbol, self.params.timeframe, self.params.candles + 1)
            # A candle is closed because its time is over, not because it is not the last row: the exchange may or may not have
            # listed the new forming candle yet. Anything not yet over is dropped, and if the newest closed candle is not the
            # one that just ended, the data is stale and nothing is evaluated (a signal on an old bar would be traded now).
            tf = pd.Timedelta(seconds=ccxt.Exchange.parse_timeframe(self.params.timeframe))
            now = pd.Timestamp(self.clock())
            now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
            closed = df[df.index + tf <= now]
            latest = now.floor(tf) - tf
            if closed.empty or closed.index[-1] != latest:
                seen = closed.index[-1] if len(closed) else "none"
                return _reject(symbol, f"stale candle data (newest closed candle {seen}, expected {latest})")
            if len(closed) < self.params.min_candles:
                return _reject(symbol, f"not enough candles ({len(closed)}<{self.params.min_candles})")
            ind = compute_indicators(closed, self.params)
            self.indicators[symbol] = ind
            from g_aegisbot.engine.strategies import evaluate_symbol     # late import: strategies imports this module
            result, decisions = evaluate_symbol(symbol, ind, len(ind) - 1, self.params)
            self.decisions.extend(decisions)
            return result
        except Exception as exc:  # one bad pair must not stop the scan
            log.warning("Scan failed for %s: %s", symbol, exc)
            return _reject(symbol, f"error: {type(exc).__name__}")

    async def scan(self) -> list[ScanResult]:
        self.decisions = []
        sem = asyncio.Semaphore(self.params.scan_concurrency)
        return list(await asyncio.gather(*(self._scan_one(sem, s) for s in self.symbols)))

    async def scan_signals(self) -> list[Signal]:
        """Signals only, best first by the configured strategy priority, then conviction, then volume."""
        return rank_signals(await self.scan(), self.params.strategy_priority)
