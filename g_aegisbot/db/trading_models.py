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
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    next_check_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # first 1m candle still to scan for stop hits
