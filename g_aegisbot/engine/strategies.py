"""Strategy registry: the two independent strategies, conflict handling and strategy-specific exits.

  breakout  Strategy A. Unchanged logic, lives in strategy.evaluate_at.
  trend     Strategy B. Trend-following pullback. Its clauses are columns built by engine/trend.py.

Both are evaluated on the same indicator frame of CLOSED candles by the live scanner and by the backtester, through
`evaluate_symbol`, so there is exactly one implementation of each rule.

Conflict rules (one position per symbol, never forced, never silent):
  * no strategy signals                 -> no trade; the per-strategy reasons are kept
  * strategies signal OPPOSITE sides    -> no trade for that symbol on that candle (they disagree), recorded as a conflict
  * strategies signal the SAME side     -> one signal is kept, chosen by strategy.signal_rank_key: STRICT strategy priority
                                           (StrategyParams.strategy_priority, default breakout then trend) first, then
                                           conviction, then volume ratio; the other is recorded as a duplicate
Strict priority applies to competing signals only. It never allows a second position on a symbol, and it never overrides
the opposite-sides rule above.
Every decision is returned as a SignalDecision so it can be logged and counted.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from g_aegisbot.engine.strategy import (
    ScanResult, Signal, StrategyParams, _lerp, _reject, breakout_candidates, evaluate_at, signal_rank_key,
)
from g_aegisbot.engine.trend import LONG_CLAUSES, SHORT_CLAUSES

BREAKOUT = "breakout"
TREND = "trend"
ALL_STRATEGIES = (BREAKOUT, TREND)

_CLAUSE_REASON = {
    "ema50": "signal candle did not close beyond EMA50 (long needs close above, short needs close below; equality fails)",
    "touch": "no pullback into EMA20 in the last 8 candles",
    "valid": "pullback broke the trend (close below EMA50 or low below EMA200)",
    "depth": "pullback too shallow (under 1 ATR)",
    "confirm": "no bullish/bearish confirmation candle",
    "vol": "volume below its 20-candle average",
}


@dataclass(frozen=True)
class SignalDecision:
    symbol: str
    strategy: str
    side: str
    kept: bool
    reason: str


def validate_strategies(names) -> tuple[str, ...]:
    names = tuple(names)
    unknown = [n for n in names if n not in ALL_STRATEGIES]
    if unknown or not names:
        raise ValueError(f"unknown or empty strategy list {names}; choose from {ALL_STRATEGIES}")
    return names


# ------------------------------------------------------------------------------------------ strategy B signal
def evaluate_trend_at(symbol: str, ind: pd.DataFrame, i: int, p: StrategyParams = StrategyParams()) -> ScanResult:
    """Apply the trend-pullback rules to closed candle i of an indicator frame."""
    if i + 1 < p.min_candles:
        return _reject(symbol, f"not enough candles ({i + 1}<{p.min_candles})")
    row = ind.iloc[i]
    a, c, vma = row["atr"], row["close"], row["vol_ma"]
    if any(pd.isna(v) for v in (a, c, vma, row["ema_f"], row["ema_s"], row["ema_p"])) or a <= 0 or vma <= 0:
        return _reject(symbol, "indicators not ready")

    if row["L_trend"]:
        side, clauses, pre = "buy", LONG_CLAUSES, "L_"
    elif row["S_trend"]:
        side, clauses, pre = "sell", SHORT_CLAUSES, "S_"
    else:
        return _reject(symbol, "no established trend")
    for name in clauses[1:]:
        if not row[name]:
            return _reject(symbol, _CLAUSE_REASON[name[len(pre):]])

    stop_dist = p.trend_stop_atr_mult * a
    stop = c - stop_dist if side == "buy" else c + stop_dist
    if stop <= 0 or stop_dist / c > p.max_stop_pct:
        return _reject(symbol, f"stop invalid or too wide ({stop_dist / c:.1%})")

    depth_atr = ((row["swing_high"] - row["win_low"]) if side == "buy" else (row["win_high"] - row["swing_low"])) / a
    vol_ratio = float(row["volume"] / vma)
    hits = int(vol_ratio >= 1.5) + int(depth_atr >= 2.0)           # two optional strength marks, no high-conviction tier
    pct = _lerp(p.normal_pct, hits / 2)
    sig = Signal(symbol, side, ind.index[i], float(c), float(a), float(stop), vol_ratio, float(row["rsi"]),
                 "normal", pct, strategy=TREND, trail_atr_mult=p.trend_trail_atr_mult)
    return ScanResult(symbol, sig, "signal")


def exit_signal(strategy: str, ind: pd.DataFrame, i: int, side: str) -> str | None:
    """A reason to close the position at the next executable price, or None. Evaluated on CLOSED candles only.
    Breakout has no discretionary exit (stop and trailing stop only). Trend exits when the trend structure is lost."""
    if strategy != TREND or i < 0 or i >= len(ind):
        return None
    row = ind.iloc[i]
    if side == "buy" and bool(row["trend_exit_long"]):
        return "trend structure lost: close below EMA50"
    if side == "sell" and bool(row["trend_exit_short"]):
        return "trend structure lost: close above EMA50"
    return None


# ------------------------------------------------------------------------------------------ evaluation + conflicts
_EVALUATORS = {BREAKOUT: evaluate_at, TREND: evaluate_trend_at}


def candidate_mask(ind: pd.DataFrame, enabled=ALL_STRATEGIES) -> np.ndarray:
    """Cheap pre-filter for backtests: bars where at least one enabled strategy could possibly signal."""
    mask = np.zeros(len(ind), dtype=bool)
    if BREAKOUT in enabled:
        mask |= breakout_candidates(ind)
    if TREND in enabled:
        mask |= (ind["trend_long"] | ind["trend_short"]).to_numpy()
    return mask


def evaluate_symbol(symbol: str, ind: pd.DataFrame, i: int, p: StrategyParams = StrategyParams(), enabled=None
                    ) -> tuple[ScanResult, list[SignalDecision]]:
    """Evaluate every enabled strategy on candle i and resolve them into at most ONE signal for the symbol."""
    enabled = validate_strategies(enabled if enabled is not None else p.strategies)
    results = {sid: _EVALUATORS[sid](symbol, ind, i, p) for sid in enabled}
    found = [(sid, r.signal) for sid, r in results.items() if r.signal is not None]

    if not found:
        if len(enabled) == 1:
            return results[enabled[0]], []                              # unchanged message for the single-strategy case
        return _reject(symbol, "; ".join(f"{sid}: {r.reason}" for sid, r in results.items())), []

    decisions: list[SignalDecision] = []
    if len({sig.side for _, sig in found}) > 1:                          # strategies disagree on direction
        detail = ", ".join(f"{sid} {sig.side}" for sid, sig in found)
        reason = f"conflict: opposite signals ({detail}); no trade"
        decisions = [SignalDecision(symbol, sid, sig.side, False, reason) for sid, sig in found]
        return _reject(symbol, reason), decisions

    ranked = sorted(found, key=lambda t: signal_rank_key(t[1], p.strategy_priority))
    keep_id, keep = ranked[0]
    decisions.append(SignalDecision(symbol, keep_id, keep.side, True, "signal"))
    for sid, sig in ranked[1:]:
        decisions.append(SignalDecision(symbol, sid, sig.side, False,
                                        f"duplicate: same symbol and direction as {keep_id}, which was kept"))
    return ScanResult(symbol, keep, "signal"), decisions
