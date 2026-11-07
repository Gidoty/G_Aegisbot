"""Persistence helpers for trade state (trailing stops) and the closed-trade log."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select

from g_aegisbot.db.trading_models import OpenTrade, TradeLog


class TradeStore:
    def __init__(self, session_factory):
        self.sf = session_factory

    def list_open(self, user_id: str) -> list[OpenTrade]:
        with self.sf() as s:
            return list(s.scalars(select(OpenTrade).where(OpenTrade.user_id == user_id)))

    def open(self, *, user_id: str, symbol: str, side: str, entry_price: float, stop_price: float,
             stop_order_id: str | None, leverage: int, mode: str, opened_at: datetime,
             strategy: str = "breakout", trail_atr_mult: float | None = None) -> None:
        with self.sf() as s:
            s.execute(delete(OpenTrade).where(OpenTrade.user_id == user_id, OpenTrade.symbol == symbol))
            s.add(OpenTrade(user_id=user_id, symbol=symbol, side=side, entry_price=entry_price,
                            stop_price=stop_price, extreme_price=entry_price, stop_order_id=stop_order_id,
                            leverage=leverage, mode=mode, opened_at=opened_at, strategy=strategy,
                            trail_atr_mult=trail_atr_mult))
            s.commit()

    def update_trail(self, trade_id: int, *, stop_price: float, extreme_price: float,
                     last_trail_ts: datetime | None, stop_order_id: str | None = None) -> None:
        with self.sf() as s:
            t = s.get(OpenTrade, trade_id)
            if t is None:
                return
            t.stop_price, t.extreme_price, t.last_trail_ts = stop_price, extreme_price, last_trail_ts
            if stop_order_id is not None:
                t.stop_order_id = stop_order_id
            s.commit()

    def remove(self, trade_id: int) -> None:
        with self.sf() as s:
            s.execute(delete(OpenTrade).where(OpenTrade.id == trade_id))
            s.commit()

    def log_closed(self, **fields) -> None:
        with self.sf() as s:
            s.add(TradeLog(**fields))
            s.commit()
