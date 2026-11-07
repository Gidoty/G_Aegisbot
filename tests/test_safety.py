"""Kill switch, protection of unmanaged positions, stop verification, lapsed subscriptions, position mode."""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import ccxt.async_support as ccxt
import pytest
from sqlalchemy import select

from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.repository import ProfileRepository
from g_aegisbot.db.trading_models import OpenTrade, PaperPosition, TradeLog
from g_aegisbot.engine import runner as runner_mod
from g_aegisbot.engine.controls import GLOBAL, ControlStore
from g_aegisbot.engine.paper import PaperExchange
from g_aegisbot.exchange import BinanceFuturesAdapter, PositionModeError
from g_aegisbot.exchange.binance_futures import PositionModeUnreadable
from tests.fakes import minute_candles
from tests.test_runner import SYM, Clock, hourly_with_forming, open_trades, positions, world  # noqa: F401


def quiet_1m(world):
    world["market"].candles[(SYM, "1m")] = minute_candles("2026-01-01 00:00", [(1, 1, 1, 1)])


def add_user(world, user_id, **fields):
    with world["sf"]() as s:
        repo = ProfileRepository(s, world["box"])
        p = repo.create(user_id, active_coins=[SYM], **fields)
        repo.set_subscription(p, "active", datetime(2100, 1, 1, tzinfo=timezone.utc))
    return p


def raw_position(sf, user="alice", symbol=SYM, side="buy", amount=1.0, entry=120.0, stop=None):
    with sf() as s:
        s.add(PaperPosition(user_id=user, symbol=symbol, side=side, amount=amount, entry_price=entry, leverage=3,
                            margin=amount * entry / 3, entry_fee=0.0, stop_price=stop,
                            stop_order_id="paper-stop-x" if stop else None,
                            opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                            next_check_ts=datetime(2100, 1, 1, tzinfo=timezone.utc)))
        s.commit()


# ------------------------------------------------------------------ control store
def test_control_scopes_and_precedence(world):
    cs = ControlStore(world["sf"])
    assert not cs.effective("alice").paused
    cs.set("alice", reason="maintenance on my account", by="admin")
    assert cs.effective("alice").paused and not cs.effective("bob").paused
    cs.set(GLOBAL, flatten=True, reason="drill")
    e = cs.effective("bob")
    assert e.paused and e.flatten and e.scope == GLOBAL and "all accounts: drill" == e.describe()
    cs.clear(GLOBAL)
    assert not cs.effective("bob").paused and cs.effective("alice").paused
    cs.clear("alice")
    assert cs.active() == []


def test_flatten_implies_pause_and_reason_is_bounded(world):
    cs = ControlStore(world["sf"])
    cs.set(GLOBAL, paused=False, flatten=True, reason="x" * 500)
    e = cs.effective("anyone")
    assert e.paused and e.flatten and len(e.reason) == 200


# ------------------------------------------------------------------ pause
async def test_global_pause_blocks_new_trades_and_says_why(world, caplog):
    r = world["runner"]
    r.controls.set(GLOBAL, reason="testing the switch")
    await r.start()
    with caplog.at_level(logging.INFO):
        await r.run_cycle()
    assert positions(world["sf"]) == []
    assert "TRADING PAUSED by the administrator (all accounts: testing the switch)" in caplog.text
    assert "keep their stop-loss orders" in caplog.text


async def test_user_pause_affects_only_that_user(world):
    add_user(world, "bob")
    r = world["runner"]
    r.controls.set("alice", reason="paused")
    await r.start()
    await r.run_cycle()
    assert {p.user_id for p in positions(world["sf"])} == {"bob"}


async def test_pause_keeps_managing_open_positions(world):
    r = world["runner"]
    await r.start()
    await r.run_cycle()
    assert len(positions(world["sf"])) == 1
    r.controls.set(GLOBAL, reason="pause")
    world["clock"].now += timedelta(hours=1)
    await r.run_cycle()
    assert len(positions(world["sf"])) == 1 and positions(world["sf"])[0].stop_price is not None


# ------------------------------------------------------------------ flatten
async def test_flatten_closes_everything_cancels_stops_and_blocks_reentry_until_cleared(world, caplog):
    r = world["runner"]
    quiet_1m(world)
    await r.start()
    await r.run_cycle()
    assert len(positions(world["sf"])) == 1 and len(open_trades(world["sf"])) == 1

    r.controls.set(GLOBAL, flatten=True, reason="emergency drill", by="admin")
    with caplog.at_level(logging.WARNING):
        await r.risk_pass()                                           # the minute loop reacts, not the hourly one
    assert positions(world["sf"]) == [] and open_trades(world["sf"]) == []
    assert "EMERGENCY FLATTEN (all accounts: emergency drill)" in caplog.text
    with world["sf"]() as s:
        assert s.scalar(select(TradeLog)).exit_reason == "market close"

    await r.run_cycle()                                               # the signal is still there, but flatten blocks it
    assert positions(world["sf"]) == []

    r.controls.clear(GLOBAL)
    await r.run_cycle()
    assert len(positions(world["sf"])) == 1                           # normal trading resumes


async def test_flatten_dry_run_closes_nothing(world):
    r = world["runner"]
    quiet_1m(world)
    await r.start()
    await r.run_cycle()
    r.dry_run = True
    r.controls.set(GLOBAL, flatten=True)
    await r.risk_pass()
    assert len(positions(world["sf"])) == 1


# ------------------------------------------------------------------ protection of unmanaged positions
async def test_position_with_no_stop_and_no_record_gets_an_emergency_stop(world, caplog):
    r = world["runner"]
    quiet_1m(world)
    world["market"].prices[SYM] = 125.0
    raw_position(world["sf"], entry=120.0)
    await r.start()
    with caplog.at_level(logging.WARNING):
        await r.risk_pass()
    pos, trade = positions(world["sf"])[0], open_trades(world["sf"])[0]
    assert pos.stop_price == pytest.approx(120 * 0.97)                # no candles loaded yet: 3% fallback
    assert trade.stop_price == pytest.approx(pos.stop_price) and trade.stop_order_id == pos.stop_order_id
    assert "SAFETY" in caplog.text and "no stop-loss and no tracking record" in caplog.text


async def test_emergency_stop_uses_atr_when_candles_are_loaded(world):
    r = world["runner"]
    quiet_1m(world)
    world["market"].prices[SYM] = 125.0
    await r.start()
    await r.run_cycle(allow_entries=False)                            # loads indicators, opens nothing
    atr = float(r.scanner.indicators[SYM]["atr"].iloc[-1])
    raw_position(world["sf"], entry=120.0)
    await r.risk_pass()
    assert positions(world["sf"])[0].stop_price == pytest.approx(120 - 2 * atr)


async def test_existing_stop_without_a_record_is_adopted_not_replaced(world):
    r = world["runner"]
    quiet_1m(world)
    world["market"].prices[SYM] = 125.0
    raw_position(world["sf"], entry=120.0, stop=110.0)
    await r.start()
    await r.risk_pass()
    assert positions(world["sf"])[0].stop_price == 110.0
    assert open_trades(world["sf"])[0].stop_price == 110.0 and open_trades(world["sf"])[0].stop_order_id == "paper-stop-x"


async def test_missing_stop_on_a_managed_trade_is_replaced_at_the_recorded_level(world, caplog):
    r = world["runner"]
    quiet_1m(world)
    await r.start()
    await r.run_cycle()
    recorded = open_trades(world["sf"])[0].stop_price
    with world["sf"]() as s:                                          # someone cancelled the stop order
        p = s.scalar(select(PaperPosition))
        p.stop_price = p.stop_order_id = None
        s.commit()
    with caplog.at_level(logging.WARNING):
        await r.risk_pass()
    assert positions(world["sf"])[0].stop_price == pytest.approx(recorded)
    assert "its stop-loss order was missing" in caplog.text


async def test_missing_stop_when_price_is_already_through_it_closes_the_position(world, caplog):
    r = world["runner"]
    quiet_1m(world)
    await r.start()
    await r.run_cycle()
    with world["sf"]() as s:
        p = s.scalar(select(PaperPosition))
        p.stop_price = p.stop_order_id = None
        s.commit()
    world["market"].prices[SYM] = open_trades(world["sf"])[0].stop_price - 1
    with caplog.at_level(logging.WARNING):
        await r.risk_pass()
    assert positions(world["sf"]) == [] and open_trades(world["sf"]) == []
    assert "already through the stop" in caplog.text


async def test_positions_in_other_symbols_are_left_alone(world):
    r = world["runner"]
    quiet_1m(world)
    world["market"].prices["ZZZ/USDT:USDT"] = 10.0
    raw_position(world["sf"], symbol="ZZZ/USDT:USDT", entry=10.0)
    await r.start()
    await r.risk_pass()
    assert positions(world["sf"])[0].stop_price is None and open_trades(world["sf"]) == []


async def test_adoption_can_be_switched_off(world):
    r = world["runner"]
    r.adopt_unmanaged = False
    quiet_1m(world)
    world["market"].prices[SYM] = 125.0
    raw_position(world["sf"], entry=120.0)
    await r.start()
    await r.risk_pass()
    assert positions(world["sf"])[0].stop_price is None


# ------------------------------------------------------------------ lapsed subscription
async def test_lapsed_subscription_blocks_entries_only(world, caplog):
    with world["sf"]() as s:
        repo = ProfileRepository(s, world["box"])
        repo.set_subscription(s.scalar(select(UserProfile)), "expired", None)
    r = world["runner"]
    await r.start()
    with caplog.at_level(logging.INFO):
        await r.run_cycle()
    assert positions(world["sf"]) == []
    assert "managing existing positions only" in caplog.text


async def test_lapsed_subscription_still_gets_stop_checks_and_the_breaker(world):
    r = world["runner"]
    await r.start()
    quiet_1m(world)
    await r.run_cycle()
    stop = positions(world["sf"])[0].stop_price
    with world["sf"]() as s:
        ProfileRepository(s, world["box"]).set_subscription(s.scalar(select(UserProfile)), "expired", None)
    now = world["clock"].now
    start = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    world["market"].candles[(SYM, "1m")] = minute_candles(start, [(stop + 2, stop + 2, stop - 3, stop - 1)] * 3)
    world["clock"].now = now + timedelta(minutes=10)
    await r.risk_pass()
    assert positions(world["sf"]) == []                               # the stop still fired for the lapsed account


# ------------------------------------------------------------------ live wiring and position mode
class FakeLive(PaperExchange):
    """Stands in for the real adapter in the runner's live branch."""
    fail_mode = None
    timeout_after_fill = False

    async def assert_one_way_mode(self):
        if self.fail_mode:
            raise self.fail_mode

    async def market_order(self, symbol, side, amount, reduce_only=False, **kw):
        res = await super().market_order(symbol, side, amount, reduce_only, **kw)
        if self.timeout_after_fill and not reduce_only:
            raise ccxt.RequestTimeout("timed out after the fill")
        return res


def go_live(world, monkeypatch, **attrs):
    with world["sf"]() as s:
        repo = ProfileRepository(s, world["box"])
        p = s.scalar(select(UserProfile))
        p.trading_mode = "live"
        repo.set_credentials(p, "KEY", "SECRET")
        repo.set_wallet_cap(p, Decimal("10000"))
    seen = {}

    def from_profile(profile, key, secret):
        seen["creds"] = (key, secret)
        a = FakeLive(world["sf"], profile.user_id, world["market"], starting_balance=10_000.0, clock=world["clock"])
        for k, v in attrs.items():
            setattr(a, k, v)
        return a

    monkeypatch.setattr(runner_mod.BinanceFuturesAdapter, "from_profile", staticmethod(from_profile))
    return seen


async def test_live_branch_decrypts_credentials_and_trades(world, monkeypatch):
    seen = go_live(world, monkeypatch)
    r = world["runner"]
    await r.start()
    await r.run_cycle()
    assert seen["creds"] == ("KEY", "SECRET")
    assert len(positions(world["sf"])) == 1 and open_trades(world["sf"])[0].mode == "live"


async def test_hedge_mode_account_is_refused_with_a_clear_message(world, monkeypatch, caplog):
    go_live(world, monkeypatch, fail_mode=PositionModeError("This Binance account uses Hedge Mode. Switch it"))
    r = world["runner"]
    await r.start()
    with caplog.at_level(logging.ERROR):
        await r.run_cycle()
        await r.risk_pass()
    assert positions(world["sf"]) == [] and "Hedge Mode" in caplog.text
    assert "Traceback" not in caplog.text


async def test_order_timeout_after_a_fill_still_ends_with_a_protected_position(world, monkeypatch, caplog):
    go_live(world, monkeypatch, timeout_after_fill=True)
    r = world["runner"]
    await r.start()
    with caplog.at_level(logging.WARNING):
        await r.run_cycle()
    pos = positions(world["sf"])
    assert len(pos) == 1 and pos[0].stop_price is not None            # filled, errored, then protected
    assert open_trades(world["sf"]) and "SAFETY" in caplog.text


async def test_position_mode_check_reads_binance_dual_side_flag():
    a = BinanceFuturesAdapter("k", "s")
    for flag, expect_error in ((True, True), ("true", True), (False, False), ("false", False)):
        async def dual(flag=flag):
            return {"dualSidePosition": flag}
        a.exchange = SimpleNamespace(fapiPrivateGetPositionSideDual=dual)
        if expect_error:
            with pytest.raises(PositionModeError, match="Hedge Mode"):
                await a.assert_one_way_mode()
        else:
            await a.assert_one_way_mode()


def _mode_adapter(response):
    a = BinanceFuturesAdapter("k", "s")

    async def dual():
        return response
    a.exchange = SimpleNamespace(fapiPrivateGetPositionSideDual=dual)
    return a


@pytest.mark.parametrize("flag", [False, "false", "False", "FALSE"])
async def test_an_explicit_false_confirms_one_way_mode(flag):
    await _mode_adapter({"dualSidePosition": flag}).assert_one_way_mode()


@pytest.mark.parametrize("flag", [True, "true", "True"])
async def test_an_explicit_true_is_hedge_mode_and_is_refused(flag):
    with pytest.raises(PositionModeError, match="Hedge Mode"):
        await _mode_adapter({"dualSidePosition": flag}).assert_one_way_mode()


@pytest.mark.parametrize("response", [
    {}, {"other": 1}, {"dualSidePosition": None}, {"dualSidePosition": "garbage"}, {"dualSidePosition": ""},
    {"dualSidePosition": " false"}, {"dualSidePosition": "0"}, {"dualSidePosition": 0}, {"dualSidePosition": 1},
    {"dualSidePosition": 0.0}, {"dualSidePosition": []}, {"dualSidePosition": ["false"]}, {"dualSidePosition": {}},
    {"dualSidePosition": {"v": False}}, None, [], "false", 5,
])
async def test_a_missing_or_unexpected_answer_fails_closed_and_is_never_read_as_one_way(response):
    with pytest.raises(PositionModeUnreadable):
        await _mode_adapter(response).assert_one_way_mode()


# ------------------------------------------------------------------ real adapter: stop order discovery
async def test_fetch_stop_orders_keeps_only_reduce_only_stops_and_merges_trigger_listing():
    """Same intent as before, but driven through the real ccxt parser (see tests/test_adapter_real_ccxt.py for the rest)."""
    from tests.ccxt_real import algo_stop, make, regular_limit, regular_stop
    a, rec = make({"regular": [regular_limit(orderId=1), regular_stop(orderId=2, stopPrice="96000"),
                               regular_stop(orderId=3, reduceOnly=False, side="BUY", stopPrice="90000")],
                   "algo": [algo_stop(algoId=9, triggerPrice="95000")]})
    found = await a.fetch_stop_orders("BTC/USDT:USDT")
    assert {o["id"] for o in found} == {"2", "9"} and rec.names() == ["fapiPrivateGetOpenOrders", "fapiPrivateGetOpenAlgoOrders"]
    assert {o["id"]: o["stopPrice"] for o in found} == {"2": 96000.0, "9": 95000.0}


async def test_fetch_stop_orders_never_hides_a_failed_listing():
    """Before: an unsupported conditional listing was skipped, which made every algo stop invisible. Now any listing
    failure propagates, so the account is treated as unverified instead of 'has no stops'."""
    import pytest as _pytest
    from tests.ccxt_real import make
    for method in ("fapiPrivateGetOpenAlgoOrders", "fapiPrivateGetOpenOrders"):
        for failure in (ccxt.BadRequest("unsupported"), ccxt.RequestTimeout("timeout")):
            a, _ = make(**{method: failure})
            with _pytest.raises(type(failure)):
                await a.fetch_stop_orders("BTC/USDT:USDT")


# ------------------------------------------------------------------ dashboard data written by the runner
async def test_runner_samples_equity_every_five_minutes_and_writes_a_heartbeat(world):
    from g_aegisbot.db.trading_models import EquitySnapshot, Heartbeat
    r = world["runner"]
    quiet_1m(world)
    await r.start()
    await r.risk_pass()
    await r.risk_pass()                                              # same minute: no second sample
    with world["sf"]() as s:
        assert len(list(s.scalars(select(EquitySnapshot)))) == 1
        beat = s.get(Heartbeat, "runner")
        assert beat is not None and beat.info == "risk pass ok"
    world["clock"].now += timedelta(minutes=5, seconds=1)
    await r.risk_pass()
    with world["sf"]() as s:
        snaps = list(s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.id)))
    assert len(snaps) == 2 and snaps[0].equity == pytest.approx(10_000)


async def test_old_equity_samples_are_pruned(world):
    from g_aegisbot.db.trading_models import EquitySnapshot
    old = world["clock"].now - timedelta(days=120)
    with world["sf"]() as s:
        s.add(EquitySnapshot(user_id="alice", at=old, equity=1.0))
        s.commit()
    r = world["runner"]
    quiet_1m(world)
    await r.start()
    await r.risk_pass()
    with world["sf"]() as s:
        assert [x.equity for x in s.scalars(select(EquitySnapshot))] == [pytest.approx(10_000)]
