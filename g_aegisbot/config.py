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
MIN_LEVERAGE = 3
DEFAULT_LEVERAGE = 3
MAX_LEVERAGE = 5
DEFAULT_TESTNET_BALANCE = Decimal("50000")

# Default watchlist: 15 liquid USDT-margined perpetuals in CCXT unified symbol format.
DEFAULT_COINS: tuple[str, ...] = (
    "BTC/USDT:USDT", "ETH/USDT:USDT", "BNB/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT",
    "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT", "LINK/USDT:USDT", "DOT/USDT:USDT",
    "LTC/USDT:USDT", "TRX/USDT:USDT", "NEAR/USDT:USDT", "ATOM/USDT:USDT", "APT/USDT:USDT",
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
