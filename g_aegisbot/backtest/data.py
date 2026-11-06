"""Historical candle download (Binance USDT-M futures via CCXT), CSV cache, and synthetic data."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def cache_path(cache_dir: str | Path, symbol: str, timeframe: str) -> Path:
    return Path(cache_dir) / f"{symbol.replace('/', '_').replace(':', '_')}_{timeframe}.csv"


def to_frame(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLUMNS).drop_duplicates("timestamp").sort_values("timestamp")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("timestamp").astype(float)


async def fetch_history(exchange, symbol: str, timeframe: str, since_ms: int, until_ms: int | None = None,
                        page: int = 1500, max_pages: int = 200) -> pd.DataFrame:
    """Page through fetch_ohlcv from `since_ms`. The still-forming candle is dropped."""
    tf_ms = int(exchange.parse_timeframe(timeframe) * 1000)
    until_ms = until_ms or int(time.time() * 1000)
    rows, since = [], since_ms
    for _ in range(max_pages):
        batch = await exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=page)
        if not batch:
            break
        rows.extend(batch)
        since = batch[-1][0] + tf_ms
        if len(batch) < page or since >= until_ms:
            break
    if not rows:
        return pd.DataFrame(columns=COLUMNS[1:], index=pd.DatetimeIndex([], tz="UTC"))
    df = to_frame(rows)
    return df[df.index + pd.Timedelta(milliseconds=tf_ms) <= pd.Timestamp(until_ms, unit="ms", tz="UTC")]


async def load_or_download(exchange, symbol: str, timeframe: str, years: float, cache_dir: str | Path,
                           refresh: bool = False) -> pd.DataFrame:
    path = cache_path(cache_dir, symbol, timeframe)
    now_ms = int(time.time() * 1000)
    tf_ms = int(exchange.parse_timeframe(timeframe) * 1000)
    since = now_ms - int(years * 365.25 * 86400 * 1000)
    if path.exists() and not refresh:
        df = _read_csv(path)
        ms = lambda ts: int(ts.timestamp() * 1000)
        if len(df) and ms(df.index[-1]) >= now_ms - 2 * 86_400_000 and ms(df.index[0]) <= since + 7 * 86_400_000:
            return df[df.index >= pd.Timestamp(since, unit="ms", tz="UTC")]
    df = await fetch_history(exchange, symbol, timeframe, since, now_ms)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.index = (out.index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(milliseconds=1)   # epoch ms, any resolution
    out.index.name = "timestamp"
    out.to_csv(path)
    return df


def _read_csv(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path)
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], unit="ms", utc=True)
    return raw.set_index("timestamp").astype(float)


async def download_all(symbols: list[str], timeframe: str, years: float, cache_dir: str | Path,
                       refresh: bool = False) -> dict[str, pd.DataFrame]:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    frames: dict[str, pd.DataFrame] = {}
    try:
        await ex.load_markets()
        for s in symbols:
            if s not in ex.markets:
                print(f"  {s}: not listed on Binance Futures, skipped")
                continue
            frames[s] = await load_or_download(ex, s, timeframe, years, cache_dir, refresh)
            f = frames[s]
            print(f"  {s}: {len(f)} candles" + (f", {f.index[0]:%Y-%m-%d} to {f.index[-1]:%Y-%m-%d}" if len(f) else ""))
    finally:
        await ex.close()
    return frames


def synthetic_ohlcv(n: int, seed: int, *, start: str = "2024-01-01", freq: str = "1h", drift: float = 0.0,
                    vol: float = 0.006, start_price: float = 100.0) -> pd.DataFrame:
    """Random-walk candles with volatility clustering and volume that rises with move size.
    For testing the pipeline only. A strategy with no edge should NOT make money on it."""
    rng = np.random.default_rng(seed)
    regime = np.exp(np.cumsum(rng.normal(0, 0.05, n)) * 0.3)
    rets = rng.normal(drift, vol, n) * regime
    close = start_price * np.cumprod(1 + rets)
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.normal(0, vol * 0.5, (n, 2))) * regime[:, None]
    high = np.maximum(open_, close) * (1 + wick[:, 0])
    low = np.minimum(open_, close) * (1 - wick[:, 1])
    volume = rng.lognormal(0, 0.4, n) * 100 * (1 + 3 * np.abs(rets) / vol)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)
