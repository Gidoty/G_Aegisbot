"""Entry price guard: an order is sent only if the price has not run away from the signal candle's close.

Why: size and stop are fixed from the signal candle's close (entry = close, stop = close -/+ 2 ATR). The market order fills
later. If the price has already run past the stop, the exchange refuses the stop order and the bot would have to close the
position it just opened; if it has run far in the trade's favour, the stop sits much further from the fill than the size
assumed. One rule, used by the live planner and by the backtester (next bar's open)."""
from dataclasses import replace

import pytest

from g_aegisbot.backtest.simulator import Backtester, BacktestConfig
from g_aegisbot.engine.strategy import Skip, StrategyParams, entry_price_problem, plan_trade
from tests.test_backtest import BASE, SYM, breakout_frame
from tests.test_strategy import FakeAdapter, P, profile, signal


def sig(side="buy", entry=100.0, atr=2.0):
    stop = entry - 2 * atr if side == "buy" else entry + 2 * atr
    return replace(signal(price=entry), side=side, entry_price=entry, atr=atr, stop_price=stop)


# ------------------------------------------------------------------------------------------------ the rule
@pytest.mark.parametrize("side,price,ok", [
    ("buy", 100.0, True), ("buy", 99.0, True), ("buy", 100.9, True),             # at or near the close, or better
    ("buy", 101.0, True),                                                          # exactly 0.5 ATR above: allowed
    ("buy", 101.01, False),                                                        # just over 0.5 ATR: chasing
    ("buy", 96.01, True), ("buy", 96.0, False), ("buy", 90.0, False),              # the stop is 96: at or through it is refused
    ("sell", 100.0, True), ("sell", 101.0, True), ("sell", 99.0, True),
    ("sell", 99.0 - 0.01, False), ("sell", 103.99, True), ("sell", 104.0, False), ("sell", 110.0, False),
])
def test_entry_price_rule(side, price, ok):
    why = entry_price_problem(sig(side), price, P)
    assert (why is None) is ok, why


def test_the_chase_limit_can_be_switched_off_but_the_stop_check_stays():
    p = replace(P, max_entry_chase_atr=0.0)
    assert entry_price_problem(sig("buy"), 150.0, p) is None
    assert entry_price_problem(sig("buy"), 95.0, p) is not None


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_an_unusable_price_is_refused(price):
    assert entry_price_problem(sig("buy"), price, P) is not None


def test_the_default_limit_is_half_an_atr():
    assert StrategyParams().max_entry_chase_atr == 0.5


# ------------------------------------------------------------------------------------------------ live planner
class Priced(FakeAdapter):
    def __init__(self, price, **kw):
        super().__init__(**kw)
        self.price = price

    async def fetch_last_price(self, symbol):
        if isinstance(self.price, Exception):
            raise self.price
        return self.price


async def test_the_planner_sizes_a_trade_whose_price_is_near_the_signal():
    s = signal(price=50_000.0)
    plan = await plan_trade(profile(), Priced(50_000.0), s, P)
    assert not isinstance(plan, Skip)


async def test_the_planner_skips_when_the_price_has_run_away_and_places_nothing():
    s = signal(price=50_000.0)
    adapter = Priced(50_000.0 + 0.6 * s.atr)
    plan = await plan_trade(profile(), adapter, s, P)
    assert isinstance(plan, Skip) and "ATR beyond the signal close" in plan.reason and adapter.calls == []


async def test_the_planner_skips_when_the_price_is_already_through_the_stop():
    s = signal(price=50_000.0)
    plan = await plan_trade(profile(), Priced(s.stop_price - 1.0), s, P)
    assert isinstance(plan, Skip) and "stop" in plan.reason


async def test_the_planner_fails_closed_when_the_price_cannot_be_read():
    plan = await plan_trade(profile(), Priced(ConnectionError("down")), signal(price=50_000.0), P)
    assert isinstance(plan, Skip) and "could not be read" in plan.reason


# ------------------------------------------------------------------------------------------------ backtester
def run(frame, **kw):
    return Backtester({SYM: frame}, BacktestConfig(**kw)).run()


def test_a_normal_next_open_still_enters():
    res = run(breakout_frame([(121, 121.3, 120.8, 121.0)] * 3))
    assert len(res.trades) == 1 and not res.notes["skipped_signals"].get("entry price ran away")


def test_a_next_open_that_runs_far_above_the_signal_close_is_not_chased():
    # the signal candle closes near 120.9 (scaled by breakout_frame); the next bar opens 8% higher
    res = run(breakout_frame([(130.6, 131.0, 130.2, 130.8)] * 3))
    assert res.trades == [] and res.notes["skipped_signals"]["entry price ran away"] == 1


def test_a_next_open_already_through_the_stop_is_not_entered():
    res = run(breakout_frame([(90.0, 91.0, 89.0, 90.0), (90, 91, 89, 90)]))
    assert res.trades == [] and res.notes["skipped_signals"]["entry price ran away"] == 1


def test_switching_the_chase_limit_off_restores_the_old_entry_for_the_far_open():
    p = replace(StrategyParams(), max_entry_chase_atr=0.0)
    res = run(breakout_frame([(130.6, 131.0, 130.2, 130.8)] * 3), params=p)
    assert len(res.trades) == 1
