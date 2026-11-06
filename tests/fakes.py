"""Shared offline fakes: a market-data adapter that serves canned candles and prices."""
from types import SimpleNamespace

import pandas as pd


class FakeMarket:
    def __init__(self, symbols=("BTC/USDT:USDT",)):
        self.prices: dict[str, float] = {}
        self.candles: dict[tuple[str, str], pd.DataFrame] = {}
        markets = {s: {"limits": {"amount": {"min": 0.001}, "cost": {"min": 5}}} for s in symbols}

        async def load_markets():
            return markets

        self.exchange = SimpleNamespace(
            markets=markets, load_markets=load_markets,
            amount_to_precision=lambda sym, a: f"{int(a * 1000) / 1000:.3f}")

    async def validate_symbols(self, symbols):
        return []

    async def fetch_last_price(self, symbol):
        return self.prices[symbol]

    async def fetch_ohlcv_df(self, symbol, timeframe="1h", limit=500, since=None):
        df = self.candles[(symbol, timeframe)]
        if since is not None:
            df = df[df.index >= pd.Timestamp(since, unit="ms", tz="UTC")]
        return df.tail(limit)

    async def close(self):
        pass


def minute_candles(start: str, rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """rows = (open, high, low, close) per minute starting at `start` (UTC)."""
    idx = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"]).assign(volume=1.0)
