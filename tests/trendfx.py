"""Deterministic candle sequences for the trend-pullback strategy.

`trend_frame` builds: a long steady trend, a short pullback, then one confirmation candle, so each rule can be broken
on its own. `direction=-1` builds the exact mirror (downtrend, rally, bearish confirmation)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def candles(returns, *, start_price=100.0, wick=0.001, volume=100.0, start="2026-01-01") -> pd.DataFrame:
    """OHLCV from per-bar close-to-close returns. open = previous close, wicks are a fixed fraction of price."""
    close = start_price * np.cumprod(1 + np.asarray(returns, dtype=float))
    open_ = np.concatenate([[start_price], close[:-1]])
    high = np.maximum(open_, close) * (1 + wick)
    low = np.minimum(open_, close) * (1 - wick)
    idx = pd.date_range(start, periods=len(close), freq="1h", tz="UTC")
    vol = np.full(len(close), volume, dtype=float) if np.isscalar(volume) else np.asarray(volume, dtype=float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def trend_frame(*, n_trend=560, trend_ret=0.002, pullback=(-0.004,) * 5, confirm_ret=0.012, confirm_volume=100.0,
                direction=1, volume=100.0) -> pd.DataFrame:
    """Rising (or, with direction=-1, falling) trend, a counter-trend pullback, one confirmation candle (last row)."""
    rets = [direction * trend_ret] * n_trend + [direction * r for r in pullback] + [direction * confirm_ret]
    vols = np.full(len(rets), volume, dtype=float)
    vols[-1] = confirm_volume
    return candles(rets, volume=vols)
