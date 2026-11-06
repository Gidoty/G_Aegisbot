"""Async CCXT adapter for Binance USDT-margined futures (mainnet or testnet)."""
from __future__ import annotations

import ccxt.async_support as ccxt
import pandas as pd

from g_aegisbot.config import MAX_LEVERAGE, MIN_LEVERAGE

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class BinanceFuturesAdapter:
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        testnet: bool = True,
        recv_window_ms: int = 10_000,
    ):
        self.testnet = testnet
        self.exchange = ccxt.binanceusdm(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "future",
                    "adjustForTimeDifference": True,
                    "recvWindow": recv_window_ms,
                },
            }
        )
        if testnet:
            self.exchange.set_sandbox_mode(True)

    @classmethod
    def from_profile(cls, profile, api_key: str, api_secret: str) -> "BinanceFuturesAdapter":
        """Build an adapter for a UserProfile. Testnet unless the profile is explicitly live."""
        return cls(api_key, api_secret, testnet=profile.trading_mode != "live")

    async def __aenter__(self) -> "BinanceFuturesAdapter":
        await self.exchange.load_markets()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self.exchange.close()

    # ---- market data ----
    async def validate_symbols(self, symbols: list[str]) -> list[str]:
        """Return the symbols that are NOT tradable linear perpetuals on this endpoint."""
        if not self.exchange.markets:
            await self.exchange.load_markets()
        markets = self.exchange.markets
        return [
            s for s in symbols
            if s not in markets or not markets[s].get("swap") or not markets[s].get("linear")
        ]

    async def fetch_ohlcv_df(
        self, symbol: str, timeframe: str = "15m", limit: int = 500, since: int | None = None
    ) -> pd.DataFrame:
        raw = await self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        df = pd.DataFrame(raw, columns=OHLCV_COLUMNS)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.set_index("timestamp")

    # ---- account ----
    async def fetch_usdt_balance(self) -> dict[str, float]:
        bal = await self.exchange.fetch_balance()
        usdt = bal.get("USDT", {})
        return {
            "total": float(usdt.get("total") or 0),
            "free": float(usdt.get("free") or 0),
            "used": float(usdt.get("used") or 0),
        }

    async def fetch_equity(self) -> float:
        """Margin balance = wallet balance + unrealized PnL, from Binance's account endpoint."""
        acct = await self.exchange.fapiPrivateV2GetAccount()
        return float(acct["totalMarginBalance"])

    async def fetch_last_price(self, symbol: str) -> float:
        return float((await self.exchange.fetch_ticker(symbol))["last"])

    async def fetch_open_positions(self, symbols: list[str] | None = None) -> list[dict]:
        positions = await self.exchange.fetch_positions(symbols)
        return [p for p in positions if float(p.get("contracts") or 0) != 0]

    # ---- account setup ----
    async def configure_symbol(self, symbol: str, leverage: int) -> None:
        """Set isolated margin and leverage. Leverage is clamped to the platform range."""
        if not MIN_LEVERAGE <= leverage <= MAX_LEVERAGE:
            raise ValueError(f"leverage must be {MIN_LEVERAGE}-{MAX_LEVERAGE}x, got {leverage}")
        try:
            await self.exchange.set_margin_mode("isolated", symbol)
        except ccxt.ExchangeError as exc:
            if "No need to change margin type" not in str(exc):  # already isolated
                raise
        await self.exchange.set_leverage(leverage, symbol)

    # ---- orders ----
    async def market_order(self, symbol: str, side: str, amount: float, reduce_only: bool = False) -> dict:
        params = {"reduceOnly": True} if reduce_only else {}
        return await self.exchange.create_order(symbol, "market", side, amount, None, params)

    async def stop_market(self, symbol: str, side: str, amount: float, stop_price: float) -> dict:
        """Exchange-side protective stop. Survives a bot crash. Always reduce-only."""
        return await self.exchange.create_order(
            symbol, "STOP_MARKET", side, amount, None,
            {"stopPrice": stop_price, "reduceOnly": True, "workingType": "MARK_PRICE"},
        )

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        try:
            await self.exchange.cancel_order(order_id, symbol)
        except ccxt.OrderNotFound:
            pass  # already filled or cancelled

    async def cancel_all(self, symbol: str) -> None:
        await self.exchange.cancel_all_orders(symbol)
