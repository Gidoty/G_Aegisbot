"""Breakout strategy engine: indicators, signal rules, ATR trailing stop, sizing, pre-trade checks.

Rules (all evaluated on the last CLOSED candle):
  1. Breakout: close beyond the prior N-candle high (long) or low (short).
  2. Trend: long needs close > EMA50 > EMA200; short needs close < EMA50 < EMA200.
  3. RSI band: long 50-70, short 30-50 (momentum without being stretched).
  4. Volume: candle volume >= 1.5 x the 20-candle volume average (prior 20 candles).
  5. Stop: entry -/+ 2 x ATR, trailed with a ratchet that only moves in the trade's favour.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from g_aegisbot.config import DEFAULT_COINS, DEFAULT_LEVERAGE

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


# --------------------------------------------------------------------------- indicators
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI."""
    delta = close.diff()
    avg_gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(avg_loss != 0, 100.0).where(avg_gain.notna())


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


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


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    signal: Signal | None
    reason: str  # why it was rejected, or "signal"


def _reject(symbol: str, reason: str) -> ScanResult:
    return ScanResult(symbol, None, reason)


def _lerp(rng: tuple[float, float], fraction: float) -> float:
    return rng[0] + (rng[1] - rng[0]) * max(0.0, min(1.0, fraction))


def evaluate(symbol: str, df: pd.DataFrame, p: StrategyParams = StrategyParams()) -> ScanResult:
    """Evaluate the last row of CLOSED candles (index = time; columns open/high/low/close/volume)."""
    if len(df) < p.min_candles:
        return _reject(symbol, f"not enough candles ({len(df)}<{p.min_candles})")

    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]
    ema_f, ema_s = ema(close, p.ema_fast), ema(close, p.ema_slow)
    rsi_s, atr_s = rsi(close, p.rsi_period), atr(df, p.atr_period)
    # Levels and volume average use the PRIOR candles only, so the signal candle cannot
    # lift its own benchmark.
    level_high = high.shift(1).rolling(p.breakout_lookback).max()
    level_low = low.shift(1).rolling(p.breakout_lookback).min()
    vol_ma = volume.shift(1).rolling(p.vol_period).mean()

    c, a, r, vma = close.iloc[-1], atr_s.iloc[-1], rsi_s.iloc[-1], vol_ma.iloc[-1]
    ef, es = ema_f.iloc[-1], ema_s.iloc[-1]
    if any(pd.isna(v) for v in (c, a, r, vma, ef, es, level_high.iloc[-1], level_low.iloc[-1])) or a <= 0 or vma <= 0:
        return _reject(symbol, "indicators not ready")

    if c > level_high.iloc[-1]:
        side: Side = "buy"
        level = level_high.iloc[-1]
    elif c < level_low.iloc[-1]:
        side = "sell"
        level = level_low.iloc[-1]
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

    vol_ratio = float(volume.iloc[-1] / vma)
    if vol_ratio < p.vol_mult:
        return _reject(symbol, f"volume filter ({vol_ratio:.2f}x < {p.vol_mult}x)")

    stop_dist = p.stop_atr_mult * a
    if stop_dist / c > p.max_stop_pct:
        return _reject(symbol, f"stop too wide ({stop_dist / c:.1%})")
    stop = c - stop_dist if long else c + stop_dist

    # Conviction: 4 criteria, "high" needs at least 3.
    breakout_atr = abs(c - level) / a
    ema_slope = (ef - ema_f.iloc[-6]) * (1 if long else -1)
    sweet = (55 <= r <= 65) if long else (35 <= r <= 45)
    criteria = [
        vol_ratio >= p.high_vol_mult,
        breakout_atr >= 0.5,
        ema_slope > 0 and abs(ef - es) / c >= 0.02,
        sweet,
    ]
    hits = sum(criteria)
    if hits >= 3:
        conviction, pct = "high", _lerp(p.high_pct, hits - 3)
    else:
        conviction, pct = "normal", _lerp(p.normal_pct, hits / 2)

    sig = Signal(symbol, side, df.index[-1], float(c), float(a), float(stop), vol_ratio, float(r), conviction, pct)
    return ScanResult(symbol, sig, "signal")


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


@dataclass(frozen=True)
class Skip:
    symbol: str
    reason: str


async def plan_trade(profile, adapter, sig: Signal, p: StrategyParams = StrategyParams()) -> TradePlan | Skip:
    """Size a trade and verify the account can fund it. Never raises on 'not enough money':
    it returns a Skip so the scan loop moves on to the next pair."""
    sym = sig.symbol
    if not profile.subscription_is_active():
        return Skip(sym, "subscription inactive")
    if profile.daily_loss_tracker.get("breaker_tripped"):
        return Skip(sym, "daily drawdown breaker tripped")
    cap = float(profile.max_wallet_cap_usdt or 0)
    if cap <= 0:
        return Skip(sym, "max_wallet_cap_usdt is 0 (trading blocked)")

    if await adapter.fetch_open_positions([sym]):
        return Skip(sym, "position already open")

    bal = await adapter.fetch_usdt_balance()
    allocated = min(cap, bal["total"])
    margin = allocated * sig.size_pct

    headroom = allocated - bal["used"]
    if margin > headroom:
        return Skip(sym, f"allocation headroom {headroom:.2f} < margin {margin:.2f}")
    if bal["free"] < margin * p.margin_buffer:
        return Skip(sym, f"insufficient free margin ({bal['free']:.2f} < {margin * p.margin_buffer:.2f})")

    notional = margin * p.leverage
    ex = adapter.exchange
    market = ex.markets.get(sym) if ex.markets else None
    amount = float(ex.amount_to_precision(sym, notional / sig.entry_price))
    if amount <= 0:
        return Skip(sym, "size rounds to zero")
    if market:
        min_amt = (market.get("limits", {}).get("amount", {}) or {}).get("min")
        min_cost = (market.get("limits", {}).get("cost", {}) or {}).get("min")
        if min_amt and amount < min_amt:
            return Skip(sym, f"below min amount {min_amt}")
        if min_cost and amount * sig.entry_price < min_cost:
            return Skip(sym, f"below min notional {min_cost}")

    risk = amount * abs(sig.entry_price - sig.stop_price)
    return TradePlan(sym, sig.side, amount, sig.entry_price, sig.stop_price, p.leverage,
                     amount * sig.entry_price / p.leverage, amount * sig.entry_price, risk,
                     sig.conviction, sig.size_pct)


async def execute_plan(adapter, plan: TradePlan) -> dict:
    """Set leverage, open the position, then place the exchange-side stop.
    If the stop cannot be placed, flatten immediately rather than hold an unprotected position."""
    await adapter.configure_symbol(plan.symbol, plan.leverage)
    entry = await adapter.market_order(plan.symbol, plan.side, plan.amount)
    exit_side = "sell" if plan.side == "buy" else "buy"
    try:
        stop = await adapter.stop_market(plan.symbol, exit_side, plan.amount, plan.stop_price)
    except Exception:
        log.exception("Stop placement failed for %s; closing position", plan.symbol)
        await adapter.market_order(plan.symbol, exit_side, plan.amount, reduce_only=True)
        raise
    return {"entry": entry, "stop": stop}


# --------------------------------------------------------------------------- scanner
@dataclass
class MultiPairScanner:
    adapter: object
    symbols: tuple[str, ...] | list[str] = ELITE_SYMBOLS
    params: StrategyParams = field(default_factory=StrategyParams)

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
            return evaluate(symbol, df.iloc[:-1], self.params)  # drop the still-forming candle
        except Exception as exc:  # one bad pair must not stop the scan
            log.warning("Scan failed for %s: %s", symbol, exc)
            return _reject(symbol, f"error: {type(exc).__name__}")

    async def scan(self) -> list[ScanResult]:
        sem = asyncio.Semaphore(self.params.scan_concurrency)
        return list(await asyncio.gather(*(self._scan_one(sem, s) for s in self.symbols)))

    async def scan_signals(self) -> list[Signal]:
        """Signals only, best first (high conviction, then strongest volume)."""
        sigs = [r.signal for r in await self.scan() if r.signal]
        return sorted(sigs, key=lambda s: (s.conviction != "high", -s.volume_ratio))
