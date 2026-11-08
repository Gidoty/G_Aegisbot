"""Strategy B clauses (trend-following pullback), computed for a whole candle frame at once.

Pure pandas, no imports from strategy.py. Every column is causal: it uses only the candle itself and earlier
candles, and the pullback window is always the W candles BEFORE the confirmation candle. Live trading and the
backtester read these same columns, so the rules cannot drift apart. The rules are documented in docs/STRATEGIES.md.

Long, evaluated on the close of candle i (short is the exact mirror):
  trend      EMA50 > EMA200, close > EMA200, and EMA50 higher than it was 24 candles ago
  ema50      the signal candle closes STRICTLY above EMA50 (a close equal to EMA50 fails). Judged on the signal candle itself
             (candle i), never on the execution candle after it
  touch      within candles i-8..i-1 at least one low reached EMA20 (a real pullback into the zone)
  valid      within candles i-8..i-1 no close was below EMA50 and no low was below EMA200 (trend intact)
  depth      the 20-candle high minus the pullback low is at least 1 ATR (touching an average is not enough)
  confirm    close > open, close > EMA20, close > the previous high, and RSI > 50 (momentum resumed)
  volume     volume >= 1.0 x the average of the prior 20 candles
Exit rule, besides the ATR stop: close back below EMA50 (long) or above EMA50 (short), the trend structure is lost.
The entry needs a close strictly on the trend side of EMA50 and the exit fires strictly on the other side, so a close exactly on
EMA50 neither enters nor exits.
"""
from __future__ import annotations

import pandas as pd

from g_aegisbot.engine.indicators import ema

LONG_CLAUSES = ("L_trend", "L_ema50", "L_touch", "L_valid", "L_depth", "L_confirm", "L_vol")
SHORT_CLAUSES = ("S_trend", "S_ema50", "S_touch", "S_valid", "S_depth", "S_confirm", "S_vol")


def add_trend_columns(out: pd.DataFrame, p) -> None:
    """Add EMA20 and the boolean clause columns to a frame that already has ema_f, ema_s, rsi, atr and vol_ma."""
    c, o, h, l = out["close"], out["open"], out["high"], out["low"]
    w = p.pullback_window

    def window_any(flag: pd.Series) -> pd.Series:          # any True within the w candles before this one
        return flag.shift(1).rolling(w).max().fillna(0).astype(bool)

    out["ema_p"] = ema(c, p.pull_ema)
    out["ema_f_slope"] = out["ema_f"] - out["ema_f"].shift(p.trend_slope_bars)
    swing_high = h.shift(1).rolling(p.swing_lookback).max()
    swing_low = l.shift(1).rolling(p.swing_lookback).min()
    win_low = l.shift(1).rolling(w).min()
    win_high = h.shift(1).rolling(w).max()
    out["win_low"], out["win_high"], out["swing_high"], out["swing_low"] = win_low, win_high, swing_high, swing_low
    vol_ok = out["volume"] >= p.trend_vol_mult * out["vol_ma"]

    out["L_trend"] = (out["ema_f"] > out["ema_s"]) & (c > out["ema_s"]) & (out["ema_f_slope"] > 0)
    out["L_ema50"] = c > out["ema_f"]                       # strict: equality with EMA50 does not qualify
    out["L_touch"] = window_any(l <= out["ema_p"])
    out["L_valid"] = ~window_any(c < out["ema_f"]) & ~window_any(l < out["ema_s"])
    out["L_depth"] = (swing_high - win_low) >= p.min_pullback_atr * out["atr"]
    out["L_confirm"] = (c > o) & (c > out["ema_p"]) & (c > h.shift(1)) & (out["rsi"] > p.trend_rsi_mid)
    out["L_vol"] = vol_ok

    out["S_trend"] = (out["ema_f"] < out["ema_s"]) & (c < out["ema_s"]) & (out["ema_f_slope"] < 0)
    out["S_ema50"] = c < out["ema_f"]                       # strict: equality with EMA50 does not qualify
    out["S_touch"] = window_any(h >= out["ema_p"])
    out["S_valid"] = ~window_any(c > out["ema_f"]) & ~window_any(h > out["ema_s"])
    out["S_depth"] = (win_high - swing_low) >= p.min_pullback_atr * out["atr"]
    out["S_confirm"] = (c < o) & (c < out["ema_p"]) & (c < l.shift(1)) & (out["rsi"] < p.trend_rsi_mid)
    out["S_vol"] = vol_ok

    out["trend_long"] = out[list(LONG_CLAUSES)].all(axis=1)
    out["trend_short"] = out[list(SHORT_CLAUSES)].all(axis=1)
    out["trend_exit_long"] = c < out["ema_f"]
    out["trend_exit_short"] = c > out["ema_f"]
