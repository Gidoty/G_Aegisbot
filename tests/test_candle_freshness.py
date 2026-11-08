"""Closed-candle handling in the live scanner: a candle counts as closed because its time has passed, not because it is
not the last row. The scan must never evaluate a stale bar as if it were the latest one."""
from datetime import timedelta

import pandas as pd
import pytest

from g_aegisbot.engine.strategy import MultiPairScanner, StrategyParams
from tests.test_strategy import make, with_breakout

SYM = "X/USDT:USDT"
HOUR = pd.Timedelta(hours=1)


class Feed:
    def __init__(self, frame):
        self.frame = frame
        self.exchange = None

    async def fetch_ohlcv_df(self, symbol, timeframe, limit):
        return self.frame.tail(limit)


def scanner(frame, now):
    return MultiPairScanner(Feed(frame), symbols=[SYM], params=StrategyParams(), clock=lambda: now.to_pydatetime())


def with_forming(df):
    forming = df.iloc[[-1]].copy()
    forming.index = [df.index[-1] + HOUR]
    return pd.concat([df, forming])


async def scan(frame, now):
    sc = scanner(frame, now)
    (res,) = await sc.scan()
    return res, sc


async def test_the_forming_candle_is_dropped_and_the_last_closed_one_is_evaluated():
    closed = with_breakout(make())
    now = closed.index[-1] + HOUR + pd.Timedelta(seconds=5)                  # just after the signal candle closed
    res, sc = await scan(with_forming(closed), now)
    assert res.signal is not None and res.signal.candle_time == closed.index[-1]
    assert sc.indicators[SYM].index[-1] == closed.index[-1]


async def test_a_just_closed_candle_is_kept_when_the_exchange_has_not_listed_the_new_one_yet():
    closed = with_breakout(make())                                             # no forming row at all
    now = closed.index[-1] + HOUR + pd.Timedelta(seconds=5)
    res, sc = await scan(closed, now)
    assert res.signal is not None, res.reason                                  # the old blind drop discarded this candle
    assert res.signal.candle_time == closed.index[-1]


async def test_a_candle_whose_hour_has_not_ended_is_never_treated_as_closed():
    closed = with_breakout(make())
    now = closed.index[-1] + pd.Timedelta(minutes=30)                          # the last row is still forming
    res, sc = await scan(closed, now)
    assert res.signal is None                                                  # the unfinished candle is not evaluated
    assert sc.indicators[SYM].index[-1] == closed.index[-2]                    # the newest CLOSED candle is the one before it


async def test_stale_data_is_rejected_and_not_stored():
    closed = with_breakout(make())
    now = closed.index[-1] + 3 * HOUR + pd.Timedelta(seconds=5)                # two whole candles are missing
    res, sc = await scan(with_forming(closed), now)
    assert res.signal is None and "stale" in res.reason
    assert sc.indicators == {}


async def test_exactly_on_the_boundary_the_new_candle_is_closed():
    closed = with_breakout(make())
    now = closed.index[-1] + HOUR                                              # the very second the candle closes
    res, _ = await scan(closed, now)
    assert res.signal is not None


@pytest.mark.parametrize("extra", [0, 1])
async def test_a_future_dated_row_is_not_closed(extra):
    closed = with_breakout(make())
    future = closed.iloc[[-1]].copy()
    future.index = [closed.index[-1] + (1 + extra) * HOUR]
    now = closed.index[-1] + HOUR + pd.Timedelta(seconds=5)
    res, _ = await scan(pd.concat([closed, future]), now)
    assert res.signal is not None and res.signal.candle_time == closed.index[-1]
