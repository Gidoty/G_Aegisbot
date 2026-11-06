import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import create_engine, select

from g_aegisbot.db import get_session_factory, init_db
from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.repository import ProfileRepository
from g_aegisbot.db.trading_models import OpenTrade, PaperAccount, PaperPosition
from g_aegisbot.engine.runner import TradingRunner, next_close
from g_aegisbot.security.crypto import SecretBox, generate_key
from tests.fakes import FakeMarket, minute_candles
from tests.test_strategy import make, with_breakout

SYM = "BTC/USDT:USDT"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def hourly_with_forming(df):
    """Closed candles plus one still-forming candle, as the exchange would return them."""
    forming = df.iloc[[-1]].copy()
    forming.index = [df.index[-1] + pd.Timedelta(hours=1)]
    return pd.concat([df, forming])


@pytest.fixture
def world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/r.db")
    init_db(engine)
    sf = get_session_factory(engine)
    box = SecretBox([generate_key()])
    closed = with_breakout(make())
    market = FakeMarket([SYM])
    market.candles[(SYM, "1h")] = hourly_with_forming(closed)
    market.prices[SYM] = float(closed["close"].iloc[-1])
    clock = Clock(closed.index[-1].to_pydatetime() + timedelta(hours=1, seconds=5))
    with sf() as s:
        repo = ProfileRepository(s, box)
        p = repo.create("alice", active_coins=[SYM])
        repo.set_subscription(p, "active", datetime(2100, 1, 1, tzinfo=timezone.utc))
    runner = TradingRunner(sf, box, market=market, clock=clock)
    return dict(sf=sf, box=box, closed=closed, market=market, clock=clock, runner=runner, repo_user="alice")


def positions(sf):
    with sf() as s:
        return list(s.scalars(select(PaperPosition)))


def open_trades(sf):
    with sf() as s:
        return list(s.scalars(select(OpenTrade)))


async def test_cycle_opens_paper_trade_and_records_trailing_state(world):
    r = world["runner"]
    await r.start()
    await r.run_cycle()
    pos, trades = positions(world["sf"]), open_trades(world["sf"])
    assert len(pos) == 1 and len(trades) == 1
    assert pos[0].stop_price == pytest.approx(trades[0].stop_price) and pos[0].stop_price < pos[0].entry_price
    assert pos[0].leverage == 3
    with world["sf"]() as s:
        tracker = s.scalar(select(UserProfile)).daily_loss_tracker
    assert tracker["start_equity"] == pytest.approx(10_000) and not tracker["breaker_tripped"]


async def test_dry_run_places_nothing(world, caplog):
    r = TradingRunner(world["sf"], world["box"], market=world["market"], clock=world["clock"], dry_run=True)
    await r.start()
    with caplog.at_level(logging.INFO):
        await r.run_cycle()
    assert positions(world["sf"]) == [] and "DRY RUN" in caplog.text


async def test_breaker_blocks_entries_and_explains_why(world, caplog):
    sf, clock = world["sf"], world["clock"]
    with sf() as s:                                         # account already down 5% today
        s.add(PaperAccount(user_id="alice", wallet_balance=9_500.0))
        p = s.scalar(select(UserProfile))
        p.daily_loss_tracker = {"date": clock.now.date().isoformat(), "start_equity": 10_000.0, "pnl": 0.0,
                                "loss_pct": 0.0, "breaker_tripped": False, "tripped_at": None}
        s.commit()
    r = world["runner"]
    await r.start()
    with caplog.at_level(logging.INFO):
        await r.run_cycle()
    assert positions(sf) == []
    assert "TRADING PAUSED until 00:00 UTC" in caplog.text and "down 5.00% today" in caplog.text
    with sf() as s:
        assert s.scalar(select(UserProfile)).daily_loss_tracker["breaker_tripped"] is True
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await r.run_cycle()                                  # next candle, same day: reminder, no entry
    assert "Still paused" in caplog.text and positions(sf) == []


async def test_breaker_resumes_next_utc_day(world):
    sf, clock = world["sf"], world["clock"]
    with sf() as s:
        s.add(PaperAccount(user_id="alice", wallet_balance=9_500.0))
        p = s.scalar(select(UserProfile))
        p.daily_loss_tracker = {"date": "2000-01-01", "start_equity": 10_000.0, "pnl": -500.0,
                                "loss_pct": 0.05, "breaker_tripped": True, "tripped_at": "2000-01-01T10:00:00+00:00"}
        s.commit()
    r = world["runner"]
    await r.start()
    await r.run_cycle()                                       # tracker is from an old day: fresh baseline, trades
    assert len(positions(sf)) == 1


async def test_max_four_open_positions(world):
    symbols = [f"C{i}/USDT:USDT" for i in range(6)]
    market = FakeMarket(symbols)
    for s_ in symbols:
        market.candles[(s_, "1h")] = world["market"].candles[(SYM, "1h")]
        market.prices[s_] = world["market"].prices[SYM]
    with world["sf"]() as s:
        s.scalar(select(UserProfile)).active_coins = symbols
        s.commit()
    r = TradingRunner(world["sf"], world["box"], market=market, clock=world["clock"])
    await r.start()
    await r.run_cycle()
    assert len(positions(world["sf"])) == 4


async def test_trailing_stop_ratchets_up_on_exchange_and_in_state(world):
    sf, clock, market = world["sf"], world["clock"], world["market"]
    r = world["runner"]
    await r.start()
    await r.run_cycle()
    first_stop = positions(sf)[0].stop_price

    closed = world["closed"]
    ts = closed.index[-1] + pd.Timedelta(hours=1)
    bar = pd.DataFrame({"open": [121.0], "high": [130.0], "low": [120.5], "close": [129.0], "volume": [100.0]}, index=[ts])
    frame = pd.concat([closed, bar])
    market.candles[(SYM, "1h")] = hourly_with_forming(frame)
    market.prices[SYM] = 128.0
    clock.now = ts.to_pydatetime() + timedelta(hours=1, seconds=5)
    await r.run_cycle()

    pos, trade = positions(sf)[0], open_trades(sf)[0]
    assert pos.stop_price > first_stop + 5
    assert trade.stop_price == pytest.approx(pos.stop_price) and trade.extreme_price == 130.0
    stop_after = pos.stop_price

    clock.now += timedelta(hours=1)                          # a quiet candle must not loosen the stop
    quiet = pd.DataFrame({"open": [129.0], "high": [129.5], "low": [127.0], "close": [128.0], "volume": [100.0]},
                         index=[ts + pd.Timedelta(hours=1)])
    market.candles[(SYM, "1h")] = hourly_with_forming(pd.concat([frame, quiet]))
    await r.run_cycle()
    assert positions(sf)[0].stop_price >= stop_after


async def test_closed_position_clears_trailing_state(world):
    r = world["runner"]
    await r.start()
    await r.run_cycle()
    with world["sf"]() as s:                                 # simulate the stop having filled
        s.delete(s.scalar(select(PaperPosition)))
        s.commit()
    world["clock"].now += timedelta(hours=1)
    world["market"].candles[(SYM, "1h")] = hourly_with_forming(world["market"].candles[(SYM, "1h")].iloc[:-1])
    await r.run_cycle(allow_entries=False)
    assert open_trades(world["sf"]) == []


async def test_risk_pass_trips_breaker_between_candles(world, caplog):
    r = world["runner"]
    await r.start()
    await r.run_cycle()                                      # opens a long
    world["market"].candles[(SYM, "1m")] = minute_candles("2026-01-01 00:00", [(1, 1, 1, 1)])
    world["market"].prices[SYM] *= 0.5                       # crash: unrealized loss far beyond 4%
    with caplog.at_level(logging.WARNING):
        await r.risk_pass()
    assert "TRADING PAUSED" in caplog.text


def test_next_close():
    t = datetime(2026, 3, 5, 12, 34, 56, tzinfo=timezone.utc)
    assert next_close(t, 3600) == datetime(2026, 3, 5, 13, 0, tzinfo=timezone.utc)
    assert next_close(datetime(2026, 3, 5, 13, 0, tzinfo=timezone.utc), 3600).hour == 14


async def test_breaker_still_runs_when_paper_stop_check_fails(world, caplog):
    r = world["runner"]                                      # no 1m candles served: process_stops raises
    await r.start()
    await r.run_cycle()
    world["market"].prices[SYM] *= 0.5
    with caplog.at_level(logging.WARNING):
        await r.risk_pass()
    assert "paper stop check failed" in caplog.text and "TRADING PAUSED" in caplog.text
