from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from g_aegisbot.config import MAX_ACTIVE_COINS, MAX_LEVERAGE, MIN_LEVERAGE

_PAIR = re.compile(r"^[A-Z0-9]{2,20}/USDT:USDT$")


def _check_pairs(v: list[str] | None) -> list[str] | None:
    if v is None:
        return v
    if len(v) > MAX_ACTIVE_COINS or len(set(v)) != len(v):
        raise ValueError(f"give up to {MAX_ACTIVE_COINS} different pairs")
    for p in v:
        if not _PAIR.match(p):
            raise ValueError(f"'{p}' is not a USDT-margined perpetual such as BTC/USDT:USDT")
    return v


class KeysIn(BaseModel):
    api_key: str = Field(min_length=16, max_length=128)
    api_secret: str = Field(min_length=16, max_length=128)

    @field_validator("api_key", "api_secret")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    def __repr__(self) -> str:      # never print secrets, even in debug output
        return "KeysIn(api_key=***, api_secret=***)"


class SettingsIn(BaseModel):
    leverage: int | None = Field(default=None, ge=MIN_LEVERAGE, le=MAX_LEVERAGE)
    active_coins: list[str] | None = None

    _pairs = field_validator("active_coins")(_check_pairs)


class AdminCreateIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    is_admin: bool = False
    trading_mode: str = Field(default="testnet", pattern="^(testnet|live)$")
    max_wallet_cap_usdt: float | None = Field(default=None, ge=0)
    virtual_balance: float | None = Field(default=None, ge=0)
    leverage: int = Field(default=3, ge=MIN_LEVERAGE, le=MAX_LEVERAGE)
    active_coins: list[str] | None = None
    subscription_days: int = Field(default=0, ge=0, le=3650)

    _pairs = field_validator("active_coins")(_check_pairs)


class AdminPatchIn(BaseModel):
    max_wallet_cap_usdt: float | None = Field(default=None, ge=0)
    virtual_balance: float | None = Field(default=None, ge=0)
    leverage: int | None = Field(default=None, ge=MIN_LEVERAGE, le=MAX_LEVERAGE)
    active_coins: list[str] | None = None
    trading_mode: str | None = Field(default=None, pattern="^(testnet|live)$")
    confirm_real_money: bool = False

    _pairs = field_validator("active_coins")(_check_pairs)


class ExtendIn(BaseModel):
    days: int = Field(ge=1, le=3650)


class KillIn(BaseModel):
    user_id: str | None = None
    flatten: bool = False
    reason: str = Field(default="", max_length=200)


class ResumeIn(BaseModel):
    user_id: str | None = None
