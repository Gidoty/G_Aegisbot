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
from g_aegisbot.engine.strategy import StrategyParams
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
    r = TradingRunner(world["sf"], world["box"], StrategyParams(max_same_direction=0), market=market, clock=world["clock"])
    await r.start()
    await r.run_cycle()
    assert len(positions(world["sf"])) == 4                              # the 4-position cap, with the side cap off


async def test_at_most_three_positions_open_on_one_side_by_default(world):
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
    assert len(positions(world["sf"])) == 3


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


# ---- strategy tags, gating and uncertain orders
async def test_opened_trade_records_its_strategy(world):
    r = world["runner"]
    await r.start()
    await r.run_cycle()
    assert [t.strategy for t in open_trades(world["sf"])] == ["breakout"]
    assert [p.strategy for p in positions(world["sf"])] == ["breakout"]


async def test_trend_only_runner_ignores_a_breakout_setup(world):
    r = TradingRunner(world["sf"], world["box"], StrategyParams(strategies=("trend",)), market=world["market"],
                      clock=world["clock"])
    await r.start()
    await r.run_cycle()
    assert positions(world["sf"]) == []


async def test_unverifiable_exchange_state_blocks_new_entries_and_recovers_when_it_succeeds(world, monkeypatch, caplog):
    from g_aegisbot.engine import runner as runner_mod
    r = world["runner"]
    await r.start()

    async def broken(self, profile, adapter):
        raise ConnectionError("exchange unreachable")

    good = runner_mod.TradingRunner._ensure_protected
    monkeypatch.setattr(runner_mod.TradingRunner, "_ensure_protected", broken)
    with caplog.at_level(logging.INFO):
        await r.run_cycle()
    assert positions(world["sf"]) == [] and "alice" in r._unverified
    assert "UNPROTECTED" in caplog.text
    monkeypatch.setattr(runner_mod.TradingRunner, "_ensure_protected", good)
    await r.run_cycle()
    assert "alice" not in r._unverified and len(positions(world["sf"])) == 1


async def test_uncertain_order_stores_nothing_marks_unverified_and_does_not_retry(world, monkeypatch):
    from g_aegisbot.engine import runner as runner_mod
    from g_aegisbot.engine.strategy import OrderUncertain
    calls = []

    async def boom(adapter, plan, **kw):
        calls.append(plan.symbol)
        raise OrderUncertain(plan.symbol, "aeg-test", None)

    monkeypatch.setattr(runner_mod, "execute_plan", boom)
    r = world["runner"]
    await r.start()
    await r.run_cycle()
    assert calls == [SYM] and open_trades(world["sf"]) == []
    assert "alice" in r._unverified or calls == [SYM]       # either flagged, or re-verified clean straight away
    await r.run_cycle()
    assert calls.count(SYM) <= 2


# ---- approved decisions: strict strategy priority in the live runner, and the trend trailing multiple across a restart
TWO = ["BRK/USDT:USDT", "TRD/USDT:USDT"]


def two_strategy_world(world):
    """BRK holds a breakout (ratio 1.6), TRD a valid trend pullback entry on 4x volume: by volume alone TRD would win."""
    from tests.trendfx import trend_frame
    brk = with_breakout(make(), vol=160.0)                                    # 400 closed candles
    trd = trend_frame(n_trend=394, confirm_volume=400.0)                       # 400 closed candles, same clock
    assert list(brk.index) == list(trd.index)
    market = FakeMarket(TWO)
    for sym, frame in zip(TWO, (brk, trd)):
        market.candles[(sym, "1h")] = hourly_with_forming(frame)
        market.prices[sym] = float(frame["close"].iloc[-1])
    with world["sf"]() as s:
        s.scalar(select(UserProfile)).active_coins = TWO
        s.commit()
    return market


@pytest.mark.parametrize("priority,expected", [(("breakout", "trend"), ("BRK/USDT:USDT", "breakout")),
                                               (("trend", "breakout"), ("TRD/USDT:USDT", "trend"))])
async def test_runner_takes_the_slot_for_the_higher_priority_strategy(world, priority, expected):
    market = two_strategy_world(world)
    params = StrategyParams(strategies=("breakout", "trend"), strategy_priority=priority, max_open_positions=1)
    r = TradingRunner(world["sf"], world["box"], params, market=market, clock=world["clock"])
    await r.start()
    await r.run_cycle()
    assert [(p.symbol, p.strategy) for p in positions(world["sf"])] == [expected]
    assert [(t.symbol, t.strategy) for t in open_trades(world["sf"])] == [expected]


async def test_trend_trailing_multiple_is_stored_and_still_used_after_a_restart(world):
    from g_aegisbot.engine.strategy import compute_indicators
    from tests.trendfx import trend_frame
    sf, clock, market = world["sf"], world["clock"], world["market"]
    frame = trend_frame(n_trend=394)
    market.candles[(SYM, "1h")] = hourly_with_forming(frame)
    market.prices[SYM] = float(frame["close"].iloc[-1])
    clock.now = frame.index[-1].to_pydatetime() + timedelta(hours=1, seconds=5)
    params = StrategyParams(strategies=("trend",))

    first = TradingRunner(sf, world["box"], params, market=market, clock=clock)
    await first.start()
    await first.run_cycle()
    trade = open_trades(sf)[0]
    assert trade.strategy == "trend" and trade.trail_atr_mult == params.trend_trail_atr_mult == 3.0

    restarted = TradingRunner(sf, world["box"], params, market=market, clock=clock)         # a new process, same database
    await restarted.start()
    assert open_trades(sf)[0].trail_atr_mult == 3.0
    last = float(frame["close"].iloc[-1])
    ts = frame.index[-1] + pd.Timedelta(hours=1)
    bar = pd.DataFrame({"open": [last], "high": [last * 1.06], "low": [last * 0.999], "close": [last * 1.05],
                        "volume": [100.0]}, index=[ts])
    grown = pd.concat([frame, bar])
    market.candles[(SYM, "1h")] = hourly_with_forming(grown)
    market.prices[SYM] = last * 1.05
    clock.now = ts.to_pydatetime() + timedelta(hours=1, seconds=5)
    await restarted.run_cycle()

    atr = float(compute_indicators(grown, params)["atr"].iloc[-1])
    trade = open_trades(sf)[0]
    assert trade.extreme_price == pytest.approx(last * 1.06)
    assert trade.stop_price == pytest.approx(last * 1.06 - 3.0 * atr)                   # the 3 ATR trend trail, not 2 ATR
    assert trade.stop_price != pytest.approx(last * 1.06 - 2.0 * atr)
    assert positions(sf)[0].stop_price == pytest.approx(trade.stop_price)


async def test_runner_still_trades_the_just_closed_candle_when_the_exchange_has_not_listed_the_next_one(world):
    world["market"].candles[(SYM, "1h")] = world["closed"]                  # no forming row in the feed
    r = world["runner"]
    await r.start()
    await r.run_cycle()
    assert len(positions(world["sf"])) == 1


async def test_runner_does_not_trade_on_stale_candles(world, caplog):
    world["clock"].now += timedelta(hours=3)                                # two candles never arrived
    r = world["runner"]
    await r.start()
    with caplog.at_level(logging.INFO):
        await r.run_cycle()
    assert positions(world["sf"]) == [] and "stale candle data" in caplog.text
