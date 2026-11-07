"""Tenant model. One row per user profile (administrator + up to 10 users)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, validates

from g_aegisbot.config import (
    DEFAULT_COINS,
    DEFAULT_LEVERAGE,
    DEFAULT_TESTNET_BALANCE,
    MAX_ACTIVE_COINS,
    MAX_LEVERAGE,
    MIN_LEVERAGE,
)
from g_aegisbot.db.base import Base


def _json():
    """A fresh JSON type per column (mutable wrappers attach to the type instance)."""
    return JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_loss_tracker() -> dict:
    return {"date": None, "start_equity": None, "pnl": 0.0, "loss_pct": 0.0, "breaker_tripped": False, "tripped_at": None}


class UserProfile(Base):
    __tablename__ = "user_profiles"
    __table_args__ = (
        CheckConstraint("market_type = 'futures'", name="ck_market_type"),
        CheckConstraint("trading_mode IN ('live', 'testnet')", name="ck_trading_mode"),
        CheckConstraint(
            f"leverage BETWEEN {MIN_LEVERAGE} AND {MAX_LEVERAGE}", name="ck_leverage_range"
        ),
        CheckConstraint(
            "subscription_status IN ('active', 'expired')", name="ck_subscription_status"
        ),
        CheckConstraint("max_wallet_cap_usdt >= 0", name="ck_wallet_cap_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)   # internal profile handle, never changes
    # Supabase `sub` (immutable user id). Bound on first successful login. Identity checks use THIS, not the email.
    auth_user_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    login_email: Mapped[str | None] = mapped_column(String(254), index=True, nullable=True)   # invite and display only
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Fernet ciphertext only. Plaintext keys are never stored.
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_api_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When these exact credentials last passed the Binance permission check (UTC). NULL = never verified. Written in the same
    # commit as the ciphertext, so replacing the keys without a passing check clears it. A timestamp only: no key material.
    keys_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    market_type: Mapped[str] = mapped_column(String(16), default="futures")
    trading_mode: Mapped[str] = mapped_column(String(16), default="testnet")
    virtual_balance: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=DEFAULT_TESTNET_BALANCE
    )
    max_wallet_cap_usdt: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))
    active_coins: Mapped[list] = mapped_column(
        MutableList.as_mutable(_json()), default=lambda: list(DEFAULT_COINS)
    )
    leverage: Mapped[int] = mapped_column(Integer, default=DEFAULT_LEVERAGE)

    subscription_status: Mapped[str] = mapped_column(String(16), default="expired")
    subscription_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # {"date": "YYYY-MM-DD" (UTC), "start_equity", "pnl", "loss_pct", "breaker_tripped", "tripped_at"}
    daily_loss_tracker: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(_json()), default=_default_loss_tracker
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    @validates("active_coins")
    def _validate_coins(self, _key: str, coins: list) -> list:
        coins = list(coins)
        if len(coins) > MAX_ACTIVE_COINS:
            raise ValueError(f"active_coins is limited to {MAX_ACTIVE_COINS} pairs.")
        if len(set(coins)) != len(coins):
            raise ValueError("active_coins contains duplicates.")
        for pair in coins:
            if not isinstance(pair, str) or not pair.endswith("/USDT:USDT"):
                raise ValueError(f"'{pair}' is not a USDT-margined perpetual (e.g. BTC/USDT:USDT).")
        return coins

    @validates("leverage")
    def _validate_leverage(self, _key: str, value: int) -> int:
        if not MIN_LEVERAGE <= value <= MAX_LEVERAGE:
            raise ValueError(f"leverage must be between {MIN_LEVERAGE}x and {MAX_LEVERAGE}x.")
        return value

    @validates("trading_mode")
    def _validate_mode(self, _key: str, value: str) -> str:
        if value not in ("live", "testnet"):
            raise ValueError("trading_mode must be 'live' or 'testnet'.")
        return value

    @validates("market_type")
    def _validate_market(self, _key: str, value: str) -> str:
        if value != "futures":
            raise ValueError("Only the 'futures' market is supported.")
        return value

    @validates("subscription_status")
    def _validate_sub(self, _key: str, value: str) -> str:
        if value not in ("active", "expired"):
            raise ValueError("subscription_status must be 'active' or 'expired'.")
        return value

    @property
    def has_credentials(self) -> bool:
        return bool(self.encrypted_api_key and self.encrypted_api_secret)

    def subscription_is_active(self, now: datetime | None = None) -> bool:
        """Admins are exempt. Everyone else needs status 'active' and an unexpired date."""
        if self.is_admin:
            return True
        if self.subscription_status != "active" or self.subscription_expires_at is None:
            return False
        expires = self.subscription_expires_at
        if expires.tzinfo is None:  # SQLite returns naive datetimes
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > (now or _utcnow())

    def __repr__(self) -> str:  # never include credentials
        return (
            f"<UserProfile {self.user_id!r} mode={self.trading_mode} "
            f"leverage={self.leverage}x creds={self.has_credentials}>"
        )
