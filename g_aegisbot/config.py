"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

# Platform limits (administrator + 10 private users).
MAX_PROFILES = 11
MAX_ACTIVE_COINS = 15
MIN_LEVERAGE = 1
DEFAULT_LEVERAGE = 3
MAX_LEVERAGE = 5
# Reference figure only. Testnet trading uses whatever balance the exchange reports.
DEFAULT_TESTNET_BALANCE = Decimal("10000")

# Default watchlist, CCXT unified symbols for Binance USDT-M perpetuals.
# PEPE trades as 1000PEPE on Binance Futures (price is quoted per 1000 tokens).
# The scanner re-checks every symbol against live markets at startup and drops unknown ones.
DEFAULT_COINS: tuple[str, ...] = (
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT",
    "LINK/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT", "NEAR/USDT:USDT", "SUI/USDT:USDT",
    "DOGE/USDT:USDT", "FET/USDT:USDT", "1000PEPE/USDT:USDT", "RENDER/USDT:USDT", "DOT/USDT:USDT",
)


@dataclass(frozen=True)
class Settings:
    database_url: str
    master_keys: tuple[str, ...]


def get_settings() -> Settings:
    keys = tuple(k.strip() for k in os.getenv("AEGIS_MASTER_KEYS", "").split(",") if k.strip())
    return Settings(
        database_url=os.getenv("AEGIS_DATABASE_URL", "sqlite:///./data/aegisbot.db"),
        master_keys=keys,
    )
