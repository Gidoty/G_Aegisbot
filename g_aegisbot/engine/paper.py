"""Paper-trading ledger for testnet profiles.

Presents the same interface as BinanceFuturesAdapter, but fills orders against live public
prices with simulated fees and slippage and keeps balances and positions in the database.
No API keys are needed. Stops are checked against 1-minute candles by process_stops().
"""
from __future__ import annotations

import ccxt.async_support as ccxt
import pandas as pd
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from g_aegisbot.config import DEFAULT_LEVERAGE, DEFAULT_TESTNET_BALANCE, MAX_LEVERAGE, MIN_LEVERAGE
from g_aegisbot.costs import TAKER_FEE, slippage_rate
from g_aegisbot.db.trading_models import PaperAccount, PaperPosition, TradeLog


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo


def _next_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0) + timedelta(minutes=1)


class PaperExchange:
    def __init__(self, session_factory, user_id: str, market, *, starting_balance: float | None = None,
                 taker_fee: float = TAKER_FEE, slippage_scale: float = 1.0, clock=_utcnow):
        self.sf, self.user_id, self.market = session_factory, user_id, market
        self.taker_fee, self.slippage_scale, self.clock = taker_fee, slippage_scale, clock
        self._leverage: dict[str, int] = {}
        self._start = float(starting_balance if starting_balance is not None else DEFAULT_TESTNET_BALANCE)
        self._seq = 0
        with self.sf() as s:
            if s.get(PaperAccount, user_id) is None:
                s.add(PaperAccount(user_id=user_id, wallet_balance=self._start))
                s.commit()

    # ---- delegated market access
    @property
    def exchange(self):
        return self.market.exchange

    async def fetch_ohlcv_df(self, *a, **k):
        return await self.market.fetch_ohlcv_df(*a, **k)

    async def fetch_last_price(self, symbol: str) -> float:
        return await self.market.fetch_last_price(symbol)

    async def close(self) -> None:  # the shared market adapter is closed by its owner
        pass

    # ---- account
    def _positions(self, s) -> list[PaperPosition]:
        return list(s.scalars(select(PaperPosition).where(PaperPosition.user_id == self.user_id)))

    @staticmethod
    def _upnl(pos: PaperPosition, price: float) -> float:
        return (price - pos.entry_price) * pos.amount * (1 if pos.side == "buy" else -1)

    async def fetch_usdt_balance(self) -> dict[str, float]:
        with self.sf() as s:
            wallet = s.get(PaperAccount, self.user_id).wallet_balance
            pos = self._positions(s)
        upnl = 0.0
        for p in pos:
            upnl += self._upnl(p, await self.fetch_last_price(p.symbol))
        used = sum(p.margin for p in pos)
        total = wallet + upnl
        return {"total": total, "free": max(0.0, total - used), "used": used}

    async def fetch_equity(self) -> float:
        return (await self.fetch_usdt_balance())["total"]

    async def fetch_open_positions(self, symbols: list[str] | None = None) -> list[dict]:
        with self.sf() as s:
            pos = self._positions(s)
        return [{"symbol": p.symbol, "side": "long" if p.side == "buy" else "short", "contracts": p.amount,
                 "entryPrice": p.entry_price} for p in pos if not symbols or p.symbol in symbols]

    async def configure_symbol(self, symbol: str, leverage: int) -> None:
        if not MIN_LEVERAGE <= leverage <= MAX_LEVERAGE:
            raise ValueError(f"leverage must be {MIN_LEVERAGE}-{MAX_LEVERAGE}x, got {leverage}")
        self._leverage[symbol] = leverage

    # ---- orders
    def _order_id(self, kind: str) -> str:
        self._seq += 1
        return f"paper-{kind}-{int(self.clock().timestamp() * 1000)}-{self._seq}"

    async def market_order(self, symbol: str, side: str, amount: float, reduce_only: bool = False) -> dict:
        price = await self.fetch_last_price(symbol)
        with self.sf() as s:
            pos = s.scalar(select(PaperPosition).where(
                PaperPosition.user_id == self.user_id, PaperPosition.symbol == symbol))
            if reduce_only:
                if pos is None or pos.side == side:
                    raise ccxt.InvalidOrder("reduce-only order would not reduce a position")
                return self._close(s, pos, price, min(amount, pos.amount), "market close", is_stop=False)
            if pos is not None:
                raise ccxt.InvalidOrder(f"{symbol}: position already open")
            lev = self._leverage.get(symbol, DEFAULT_LEVERAGE)
            fill = price * (1 + slippage_rate(symbol, scale=self.slippage_scale) * (1 if side == "buy" else -1))
            notional = amount * fill
            margin, fee = notional / lev, notional * self.taker_fee
            wallet = s.get(PaperAccount, self.user_id)
            others = self._positions(s)
            used = sum(p.margin for p in others)
            if wallet.wallet_balance - used < margin + fee:   # unrealized PnL of others ignored (conservative)
                raise ccxt.InsufficientFunds("paper account: insufficient free margin")
            now = self.clock()
            s.add(PaperPosition(user_id=self.user_id, symbol=symbol, side=side, amount=amount, entry_price=fill,
                                leverage=lev, margin=margin, entry_fee=fee, opened_at=now,
                                next_check_ts=_next_minute(now)))
            wallet.wallet_balance -= fee
            s.commit()
            return {"id": self._order_id("m"), "average": fill, "filled": amount, "fee": fee, "status": "closed"}

    async def stop_market(self, symbol: str, side: str, amount: float, stop_price: float) -> dict:
        last = await self.fetch_last_price(symbol)
        if (side == "sell" and stop_price >= last) or (side == "buy" and stop_price <= last):
            raise ccxt.InvalidOrder("Order would immediately trigger")   # same rule as the exchange
        with self.sf() as s:
            pos = s.scalar(select(PaperPosition).where(
                PaperPosition.user_id == self.user_id, PaperPosition.symbol == symbol))
            if pos is None:
                raise ccxt.InvalidOrder("no position to protect")
            pos.stop_price, pos.stop_order_id = stop_price, self._order_id("stop")
            s.commit()
            return {"id": pos.stop_order_id, "stopPrice": stop_price}

    async def fetch_stop_orders(self, symbol: str) -> list[dict]:
        with self.sf() as s:
            pos = s.scalar(select(PaperPosition).where(
                PaperPosition.user_id == self.user_id, PaperPosition.symbol == symbol))
        if pos is None or pos.stop_price is None:
            return []
        return [{"id": pos.stop_order_id, "stopPrice": pos.stop_price, "side": "sell" if pos.side == "buy" else "buy"}]

    async def assert_one_way_mode(self) -> None:
        return None

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        with self.sf() as s:
            pos = s.scalar(select(PaperPosition).where(
                PaperPosition.user_id == self.user_id, PaperPosition.symbol == symbol))
            if pos is not None and pos.stop_order_id == order_id:
                pos.stop_price = pos.stop_order_id = None
                s.commit()

    async def cancel_all(self, symbol: str) -> None:
        with self.sf() as s:
            pos = s.scalar(select(PaperPosition).where(
                PaperPosition.user_id == self.user_id, PaperPosition.symbol == symbol))
            if pos is not None:
                pos.stop_price = pos.stop_order_id = None
                s.commit()

    # ---- fills
    def _close(self, s, pos: PaperPosition, price: float, amount: float, reason: str, *, is_stop: bool) -> dict:
        closing_long = pos.side == "buy"
        slip = slippage_rate(pos.symbol, is_stop=is_stop, scale=self.slippage_scale)
        fill = price * (1 - slip if closing_long else 1 + slip)
        frac = amount / pos.amount
        gross = (fill - pos.entry_price) * amount * (1 if closing_long else -1)
        fee = fill * amount * self.taker_fee
        entry_fee_share = pos.entry_fee * frac
        net = gross - fee - entry_fee_share
        wallet = s.get(PaperAccount, self.user_id)
        wallet.wallet_balance += gross - fee                 # entry fee was paid at open
        s.add(TradeLog(user_id=self.user_id, symbol=pos.symbol, side=pos.side, amount=amount,
                       entry_price=pos.entry_price, exit_price=fill, pnl=net, fees=fee + entry_fee_share,
                       opened_at=_aware(pos.opened_at), closed_at=self.clock(), exit_reason=reason, mode="testnet"))
        if frac >= 0.999999:
            s.delete(pos)
        else:
            pos.amount -= amount
            pos.margin *= 1 - frac
            pos.entry_fee *= 1 - frac
        s.commit()
        return {"id": self._order_id("c"), "average": fill, "filled": amount, "fee": fee, "pnl": net, "status": "closed"}

    async def process_stops(self) -> list[dict]:
        """Check each protected position against the 1m candles since it was last checked.
        A candle that trades through the stop closes the position at the stop (or at the open if it gapped)."""
        closed = []
        with self.sf() as s:
            positions = [p for p in self._positions(s) if p.stop_price is not None]
            snapshot = [(p.id, p.symbol, p.side, p.stop_price, _aware(p.next_check_ts)) for p in positions]
        now = self.clock()
        for pid, symbol, side, stop, nxt in snapshot:
            since = int(nxt.timestamp() * 1000)
            df = await self.fetch_ohlcv_df(symbol, "1m", limit=1000, since=since)
            df = df[df.index + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]   # completed candles only
            hit = None
            for ts, row in df.iterrows():
                if side == "buy" and row["low"] <= stop:
                    hit = min(stop, row["open"])
                elif side == "sell" and row["high"] >= stop:
                    hit = max(stop, row["open"])
                if hit is not None:
                    break
            with self.sf() as s:
                pos = s.get(PaperPosition, pid)
                if pos is None:
                    continue
                if hit is not None:
                    closed.append(self._close(s, pos, hit, pos.amount, "stop-loss", is_stop=True))
                elif len(df):
                    pos.next_check_ts = df.index[-1].to_pydatetime() + timedelta(minutes=1)
                    s.commit()
        return closed
