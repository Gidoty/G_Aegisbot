from datetime import datetime, timezone

import ccxt.async_support as ccxt
import pytest
from sqlalchemy import create_engine, select

from g_aegisbot.costs import TAKER_FEE, slippage_rate
from g_aegisbot.db import get_session_factory, init_db
from g_aegisbot.db.trading_models import PaperAccount, PaperPosition, TradeLog
from g_aegisbot.engine.paper import PaperExchange
from tests.fakes import FakeMarket, minute_candles

SYM = "BTC/USDT:USDT"


class Clock:
    now = datetime(2026, 3, 5, 12, 0, 30, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/p.db")
    init_db(engine)
    sf = get_session_factory(engine)
    market, clock = FakeMarket([SYM]), Clock()
    market.prices[SYM] = 100.0
    return sf, market, clock


def make(env, balance=10_000):
    sf, market, clock = env
    return PaperExchange(sf, "alice", market, starting_balance=balance, clock=clock)


async def test_open_and_close_long_accounting(env):
    sf, market, _ = env
    px = make(env)
    await px.configure_symbol(SYM, 3)
    o = await px.market_order(SYM, "buy", 10)
    fill = 100 * (1 + slippage_rate(SYM))
    fee_in = 10 * fill * TAKER_FEE
    assert o["average"] == pytest.approx(fill)
    bal = await px.fetch_usdt_balance()
    assert bal["used"] == pytest.approx(10 * fill / 3)
    # price unchanged: the fee is paid and the entry slippage shows as a small unrealized loss
    assert bal["total"] == pytest.approx(10_000 - fee_in + (100 - fill) * 10)

    market.prices[SYM] = 110.0
    assert (await px.fetch_equity()) == pytest.approx(10_000 - fee_in + (110 - fill) * 10)

    c = await px.market_order(SYM, "sell", 10, reduce_only=True)
    exit_fill = 110 * (1 - slippage_rate(SYM))
    fee_out = 10 * exit_fill * TAKER_FEE
    gross = (exit_fill - fill) * 10
    assert c["pnl"] == pytest.approx(gross - fee_out - fee_in)
    with sf() as s:
        assert s.get(PaperAccount, "alice").wallet_balance == pytest.approx(10_000 - fee_in + gross - fee_out)
        assert s.scalar(select(PaperPosition)) is None
        log = s.scalar(select(TradeLog))
        assert log.mode == "testnet" and log.pnl == pytest.approx(c["pnl"])


async def test_short_profits_when_price_falls(env):
    px = make(env)
    await px.configure_symbol(SYM, 3)
    await px.market_order(SYM, "sell", 10)
    env[1].prices[SYM] = 90.0
    c = await px.market_order(SYM, "buy", 10, reduce_only=True)
    assert c["pnl"] > 90          # ~100 gross minus ~1.5 of fees and slippage


async def test_insufficient_funds_and_duplicates_rejected(env):
    px = make(env, balance=100)
    with pytest.raises(ccxt.InsufficientFunds):
        await px.market_order(SYM, "buy", 10)
    px2 = make(env, balance=100_000)       # same user, existing account keeps its own balance (100)
    px3 = PaperExchange(env[0], "bob", env[1], starting_balance=100_000, clock=env[2])
    await px3.market_order(SYM, "buy", 1)
    with pytest.raises(ccxt.InvalidOrder):
        await px3.market_order(SYM, "buy", 1)


async def test_leverage_bounds(env):
    px = make(env)
    for bad in (0, 6):
        with pytest.raises(ValueError):
            await px.configure_symbol(SYM, bad)


async def test_stop_that_would_trigger_immediately_is_rejected(env):
    px = make(env)
    await px.market_order(SYM, "buy", 1)
    with pytest.raises(ccxt.InvalidOrder):
        await px.stop_market(SYM, "sell", 1, 100.5)
    o = await px.stop_market(SYM, "sell", 1, 95.0)
    o2 = await px.stop_market(SYM, "sell", 1, 96.0)       # replacing moves the stop
    await px.cancel_order(SYM, o["id"])                    # stale id: no effect
    with env[0]() as s:
        assert s.scalar(select(PaperPosition)).stop_price == 96.0
    await px.cancel_order(SYM, o2["id"])
    with env[0]() as s:
        assert s.scalar(select(PaperPosition)).stop_price is None


async def test_process_stops_hits_on_candle_low_and_fills_at_stop(env):
    sf, market, clock = env
    px = make(env)
    await px.market_order(SYM, "buy", 10)
    await px.stop_market(SYM, "sell", 10, 95.0)
    market.candles[(SYM, "1m")] = minute_candles("2026-03-05 12:00", [
        (90, 90, 80, 85),         # 12:00 candle is before the entry minute boundary: ignored
        (100, 101, 99, 100),      # 12:01 quiet
        (99, 99.5, 94, 96),       # 12:02 trades through 95
        (96, 97, 96, 97),
    ])
    clock.now = datetime(2026, 3, 5, 12, 10, tzinfo=timezone.utc)
    closed = await px.process_stops()
    assert len(closed) == 1
    assert closed[0]["average"] == pytest.approx(95 * (1 - slippage_rate(SYM, is_stop=True)))
    with sf() as s:
        assert s.scalar(select(PaperPosition)) is None
        assert s.scalar(select(TradeLog)).exit_reason == "stop-loss"


async def test_process_stops_gap_fills_at_open_and_ignores_forming_candle(env):
    sf, market, clock = env
    px = make(env)
    await px.market_order(SYM, "buy", 10)
    await px.stop_market(SYM, "sell", 10, 95.0)
    market.candles[(SYM, "1m")] = minute_candles("2026-03-05 12:01", [(100, 100, 99, 99)])
    clock.now = datetime(2026, 3, 5, 12, 1, 30, tzinfo=timezone.utc)       # 12:01 candle still forming
    assert await px.process_stops() == []
    market.candles[(SYM, "1m")] = minute_candles("2026-03-05 12:01", [(100, 100, 99, 99), (90, 91, 88, 90)])
    clock.now = datetime(2026, 3, 5, 12, 5, tzinfo=timezone.utc)
    closed = await px.process_stops()
    assert closed[0]["average"] == pytest.approx(90 * (1 - slippage_rate(SYM, is_stop=True)))   # gapped below the stop


async def test_state_survives_restart(env):
    px = make(env)
    await px.market_order(SYM, "buy", 1)
    px2 = make(env)                    # new instance, same database
    assert len(await px2.fetch_open_positions()) == 1
    assert (await px2.fetch_usdt_balance())["total"] < 10_000      # fee was paid and persisted
