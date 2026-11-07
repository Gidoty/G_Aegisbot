"""Trade state and the paper-trading ledger. Float columns: these are working figures, not accounting records."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from g_aegisbot.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OpenTrade(Base):
    """Trailing-stop state for a position the bot opened (live or paper). Survives restarts."""
    __tablename__ = "open_trades"
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_open_trade"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))                 # buy | sell
    entry_price: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float] = mapped_column(Float)
    extreme_price: Mapped[float] = mapped_column(Float)          # best price since entry
    stop_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    leverage: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(8))                 # live | testnet
    strategy: Mapped[str] = mapped_column(String(24), default="breakout", server_default="breakout")
    trail_atr_mult: Mapped[float | None] = mapped_column(Float, nullable=True)   # None: the default multiple
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_trail_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TradeLog(Base):
    __tablename__ = "trade_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    amount: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)   # net of fees; None when unknown (live)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    exit_reason: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(24), default="breakout", server_default="breakout")


class PaperAccount(Base):
    __tablename__ = "paper_accounts"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet_balance: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_paper_position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    amount: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer)
    margin: Mapped[float] = mapped_column(Float)
    entry_fee: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy: Mapped[str] = mapped_column(String(24), default="breakout", server_default="breakout")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    next_check_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # first 1m candle still to scan for stop hits


class Control(Base):
    """Kill switch. One row per scope: 'global' (everyone) or a user_id. flatten implies paused."""
    __tablename__ = "controls"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    paused: Mapped[bool] = mapped_column(default=False)
    flatten: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    set_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditLog(Base):
    """Who changed what. Never stores secrets."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(400), nullable=True)


class Payment(Base):
    """One NOWPayments invoice. `credited` flips exactly once, when the payment is fully confirmed."""
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(String(96), unique=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    invoice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="created")
    price_usd: Mapped[float] = mapped_column(Float)
    days: Mapped[int] = mapped_column(Integer)
    credited: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EquitySnapshot(Base):
    """Account equity sampled by the runner, for the dashboard chart."""
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    equity: Mapped[float] = mapped_column(Float)


class Heartbeat(Base):
    """Written by the runner so the API and dashboard can tell it is alive."""
    __tablename__ = "heartbeats"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    info: Mapped[str | None] = mapped_column(String(200), nullable=True)
