"""Runner-level safety: uncertain entries block trading, exits keep their stop until flat is confirmed, and a position
is only ever recorded as closed when the exchange shows it at zero.

A scriptable paper-backed adapter records the ORDER of exchange calls, so the tests can assert things like "the stop was
still on the exchange when the close was sent" and "the stop was cancelled only after the position was gone"."""
import logging
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pytest
from sqlalchemy import create_engine, select

from g_aegisbot.db import get_session_factory, init_db
from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.repository import ProfileRepository
from g_aegisbot.db.trading_models import OpenTrade, PaperPosition, TradeLog
from g_aegisbot.engine import runner as runner_mod
from g_aegisbot.engine.controls import GLOBAL
from g_aegisbot.engine.paper import PaperExchange
from g_aegisbot.engine.runner import TradingRunner
from g_aegisbot.security.crypto import SecretBox, generate_key
from decimal import Decimal
from tests.fakes import FakeMarket
from tests.test_runner import Clock, hourly_with_forming
from tests.test_strategy import make, with_breakout

A, B, C = "BTC/USDT:USDT", "C1/USDT:USDT", "C2/USDT:USDT"


async def no_sleep(_):
    pass


class Scripted(PaperExchange):
    """A paper exchange whose misbehaviour is switched on per test. It behaves like a REAL exchange for reconciliation
    (ledger = False) and answers every evidence query from scripted attributes."""
    ledger = False

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.events: list[tuple] = []
        self.entry_mode: dict[str, str] = {}        # symbol -> normal | canceled | timeout_nofill | timeout_fill | open_nofill
        self.close_mode = "normal"                  # normal | raise | timeout_nofill | timeout_filled | noop | half | stop_fills_first
        self.close_modes: dict[str, str] = {}       # per-symbol override
        self.fail_stop_for: set[str] = set()
        self.cancel_noop = False
        self.lookup_answers: list = [None]
        self.open_orders: list | Exception = []     # what the open-orders listing returns (or raises)
        self.conditional_orders: list | Exception = []
        self.history: list | Exception = []         # order history in the window
        self.trades: list | Exception = []          # account trades in the window
        self.position_info: dict | None = None      # extra raw fields on positions, e.g. {"positionSide": "LONG"}
        self.lookup_calls = 0
        self.cancel_by_id: list[str] = []

    # ---- helpers
    def count(self, kind: str, symbol: str | None = None, reduce_only: bool | None = None) -> int:
        return sum(1 for e in self.events if e[0] == kind and (symbol is None or e[1] == symbol)
                   and (reduce_only is None or e[2] == reduce_only))

    async def _stop_present(self, symbol):
        return bool(await PaperExchange.fetch_stop_orders(self, symbol))

    # ---- orders
    async def market_order(self, symbol, side, amount, reduce_only=False, **kw):
        self.events.append(("market", symbol, reduce_only, await self._stop_present(symbol)))
        if not reduce_only:
            mode = self.entry_mode.get(symbol, "normal")
            if mode == "canceled":
                return {"id": "x", "filled": 0, "status": "canceled"}
            if mode == "open_nofill":
                return {"id": "x", "filled": 0, "status": "open"}
            if mode == "timeout_nofill":
                raise ccxt.RequestTimeout("timed out; the order never arrived")
            if mode == "timeout_partial":                                 # only 5 units fill, then the call times out
                await super().market_order(symbol, side, 5.0, reduce_only, **kw)
                raise ccxt.RequestTimeout("timed out after a partial fill")
            res = await super().market_order(symbol, side, amount, reduce_only, **kw)
            if mode == "timeout_fill":
                raise ccxt.RequestTimeout("timed out after the fill")
            return res
        mode = self.close_modes.get(symbol, self.close_mode)
        if mode == "raise":
            raise ccxt.InvalidOrder("close rejected")
        if mode == "timeout_nofill":
            raise ccxt.RequestTimeout("timed out; nothing was closed")
        if mode == "noop":
            return {"id": "c", "filled": amount, "status": "closed"}        # claims success, closes nothing
        if mode == "half":
            return await super().market_order(symbol, side, amount / 2, reduce_only, **kw)
        if mode == "stop_fills_first":
            with self.sf() as s:                                              # the protective stop fires first
                pos = s.scalar(select(PaperPosition).where(PaperPosition.symbol == symbol))
                self._close(s, pos, pos.stop_price, pos.amount, "stop-loss", is_stop=True)
            raise ccxt.InvalidOrder("ReduceOnly Order is rejected")
        res = await super().market_order(symbol, side, amount, reduce_only, **kw)
        if mode == "timeout_filled":
            raise ccxt.RequestTimeout("timed out after the close filled")
        return res

    async def stop_market(self, symbol, side, amount, stop_price):
        self.events.append(("stop_market", symbol))
        if symbol in self.fail_stop_for:
            raise ccxt.ExchangeError("stop rejected")
        return await super().stop_market(symbol, side, amount, stop_price)

    async def cancel_all(self, symbol):
        self.events.append(("cancel_all", symbol))
        if not self.cancel_noop:
            await super().cancel_all(symbol)

    async def fetch_order_by_client_id(self, symbol, client_id):
        answer = self.lookup_answers[min(self.lookup_calls, len(self.lookup_answers) - 1)]
        self.lookup_calls += 1
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict):                     # a real exchange echoes the identity of the order it returns
            return {"clientOrderId": client_id, "symbol": symbol, **answer}
        return answer

    async def fetch_open_orders(self, symbol):
        if isinstance(self.open_orders, Exception):
            raise self.open_orders
        return list(self.open_orders)

    async def fetch_conditional_orders(self, symbol):
        if isinstance(self.conditional_orders, Exception):
            raise self.conditional_orders
        return list(self.conditional_orders)

    async def fetch_orders_history(self, symbol, since_ms, until_ms):
        if isinstance(self.history, Exception):
            raise self.history
        return list(self.history)

    async def fetch_account_trades(self, symbol, since_ms, until_ms):
        if isinstance(self.trades, Exception):
            raise self.trades
        return list(self.trades)

    async def fetch_open_positions(self, symbols=None):
        rows = await super().fetch_open_positions(symbols)
        for r in rows:
            r["info"] = dict(self.position_info or {"positionSide": "BOTH"})
        return rows

    async def cancel_order_by_client_id(self, symbol, client_id):
        self.cancel_by_id.append(client_id)


def build(tmp_path, monkeypatch, symbols=(A,)):
    """A live-mode user and a runner whose exchange adapter is a Scripted paper ledger. Returns a dict."""
    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(engine)
    sf = get_session_factory(engine)
    box = SecretBox([generate_key()])
    closed = with_breakout(make())
    market = FakeMarket(list(symbols))
    for sym in symbols:
        market.candles[(sym, "1h")] = hourly_with_forming(closed)
        market.prices[sym] = float(closed["close"].iloc[-1])
    clock = Clock(closed.index[-1].to_pydatetime() + timedelta(hours=1, seconds=5))
    with sf() as s:
        repo = ProfileRepository(s, box)
        p = repo.create("alice", active_coins=list(symbols), trading_mode="live")
        repo.set_subscription(p, "active", datetime(2100, 1, 1, tzinfo=timezone.utc))
        repo.set_credentials(p, "KEY", "SECRET")
        repo.set_wallet_cap(p, Decimal("10000"))
    holder = {}

    def from_profile(profile, key, secret):
        # the runner rebuilds its adapter whenever the profile row changes; keep one scripted exchange per test
        if "adapter" not in holder:
            holder["adapter"] = Scripted(sf, profile.user_id, market, starting_balance=10_000.0, clock=clock)
        return holder["adapter"]

    monkeypatch.setattr(runner_mod.BinanceFuturesAdapter, "from_profile", staticmethod(from_profile))
    from g_aegisbot.engine.strategy import StrategyParams
    r = TradingRunner(sf, box, StrategyParams(max_same_direction=0), market=market, clock=clock, sleep=no_sleep,
                      )
    return dict(sf=sf, runner=r, market=market, clock=clock, holder=holder)


async def prepared(w):
    """Runner started, adapter built (no cycle run yet)."""
    r = w["runner"]
    await r.start()
    profile = r._load_profiles()[0]
    adapter = await r._adapter_for(profile)
    return r, profile, adapter


def positions(sf):
    with sf() as s:
        return list(s.scalars(select(PaperPosition)))


def trades(sf):
    with sf() as s:
        return list(s.scalars(select(OpenTrade)))


def trade_log(sf):
    with sf() as s:
        return list(s.scalars(select(TradeLog)))


# =================================================================== C1 / C4: entries
async def test_a_protection_failure_stops_the_remaining_signals_in_the_same_cycle(tmp_path, monkeypatch, caplog):
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    adapter.fail_stop_for = {A, B}                 # no stop can be placed anywhere
    adapter.close_modes = {A: "raise", B: "raise"}     # and the emergency close is refused as well
    with caplog.at_level(logging.INFO):
        await r.run_cycle()
    # exactly one entry was attempted: the second signal was never processed
    assert adapter.count("market", reduce_only=False) == 1
    assert len(positions(w["sf"])) == 1                                    # the exposure that exists is the first one
    assert "alice" in r._unverified and r._entries_blocked("alice")
    assert "entry processing stopped for this cycle" in caplog.text or "NEW ENTRIES BLOCKED" in caplog.text


async def test_a_generic_exception_in_an_entry_halts_the_cycle_instead_of_opening_more(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    original = adapter.configure_symbol
    calls = []

    async def broken(symbol, leverage):
        calls.append(symbol)
        raise RuntimeError("unexpected")

    adapter.configure_symbol = broken
    await r.run_cycle()
    assert calls == [A]                                                    # the second signal was not attempted
    assert adapter.count("market", reduce_only=False) == 0
    adapter.configure_symbol = original


async def test_a_definitively_unfilled_entry_does_not_stop_the_next_signal(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    adapter.entry_mode = {A: "canceled"}
    adapter.lookup_answers = [{"id": "x", "status": "canceled", "filled": 0}]       # the exchange confirms the cancel
    await r.run_cycle()
    assert [p.symbol for p in positions(w["sf"])] == [B]                   # A provably did not open, B did
    assert not r._pending and "alice" not in r._unverified


async def test_an_entry_that_may_have_reached_the_exchange_blocks_entries_until_reconciled(tmp_path, monkeypatch, caplog):
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    adapter.entry_mode = {A: "timeout_nofill"}
    adapter.lookup_answers = [{"filled": 0, "status": "open"}]           # the order is still working on the exchange
    with caplog.at_level(logging.INFO):
        await r.run_cycle()
    entries = adapter.count("market", reduce_only=False)
    assert entries == 1 and positions(w["sf"]) == []                      # B was NOT opened behind it
    assert r._pending["alice"] and r._entries_blocked("alice")
    cid = next(iter(r._pending["alice"]))
    # further cycles neither resend the entry nor open anything while the order is unresolved
    await r.run_cycle()
    await r.risk_pass()
    assert adapter.count("market", reduce_only=False) == entries and positions(w["sf"]) == []
    assert "still unresolved" in caplog.text and adapter.cancel_by_id and set(adapter.cancel_by_id) == {cid}
    # the exchange now reports the order canceled with nothing filled and no position exists: reconciled
    adapter.lookup_answers = [{"filled": 0, "status": "canceled"}]
    await r.risk_pass()
    assert not r._pending and "alice" not in r._unverified
    adapter.entry_mode = {}
    await r.run_cycle()
    assert {p.symbol for p in positions(w["sf"])} == {A, B}                # trading resumes only now


async def test_an_order_the_exchange_has_never_seen_is_not_released_by_time_or_repetition(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    adapter.entry_mode = {A: "timeout_nofill"}
    adapter.lookup_answers = [None]                                       # the exchange has no such order (yet)
    await r.run_cycle()
    assert r._pending["alice"] and positions(w["sf"]) == []
    for _ in range(6):
        w["clock"].now += timedelta(seconds=3600)
        await r.risk_pass()
    assert r._pending["alice"], "elapsed time and repeated 'not found' answers must never release an entry"
    assert adapter.count("market", reduce_only=False) == 1                # and it was never resent


async def test_a_timeout_after_the_fill_is_found_by_client_id_protected_and_recorded(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    adapter.entry_mode = {A: "timeout_fill"}
    adapter.lookup_answers = [{"filled": 17.0, "status": "closed", "average": 120.0}]
    await r.run_cycle()
    pos = positions(w["sf"])
    assert len(pos) == 1 and pos[0].stop_price is not None and len(trades(w["sf"])) == 1
    assert adapter.count("market", reduce_only=False) == 1 and not r._pending


async def test_a_partly_filled_working_order_is_protected_and_blocks_further_entries(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    adapter.entry_mode = {A: "timeout_partial"}
    adapter.lookup_answers = [{"filled": 5.0, "status": "open"}]          # 5 filled, the rest is still working
    await r.run_cycle()
    pos = [p for p in positions(w["sf"]) if p.symbol == A]
    assert len(pos) == 1 and pos[0].amount == 5.0 and pos[0].stop_price is not None      # the filled part has its stop
    assert [t.symbol for t in trades(w["sf"])] == [A]
    assert adapter.cancel_by_id, "the remainder must be cancelled by client order id"
    assert r._pending["alice"] and not [p for p in positions(w["sf"]) if p.symbol == B]    # B was not opened behind it
    assert adapter.count("market", reduce_only=False) == 1


# =================================================================== C2 / M3: exits
async def open_one(w, symbols=(A,)):
    r, profile, adapter = await prepared(w)
    await r.run_cycle()
    assert len(positions(w["sf"])) == len(symbols)
    return r, profile, adapter


def first_trade(w, symbol=A):
    return next(t for t in trades(w["sf"]) if t.symbol == symbol)


async def test_full_close_keeps_the_stop_until_flat_then_cancels_verifies_and_only_then_records(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    r, profile, adapter = await open_one(w)
    t = first_trade(w)
    adapter.events.clear()
    await r._strategy_exit(profile, adapter, t, 1.0, "trend structure lost")
    close = next(e for e in adapter.events if e[0] == "market" and e[2] is True)
    assert close[3] is True, "the protective stop was NOT on the exchange when the close was sent"
    names = [e[0] for e in adapter.events]
    assert names.index("market") < names.index("cancel_all")              # stop cancelled only after the close
    assert positions(w["sf"]) == [] and trades(w["sf"]) == []
    log = trade_log(w["sf"])
    assert [x.exit_reason for x in log if x.mode == "live"] == ["strategy-exit: trend structure lost"]   # exactly one record


async def test_partial_close_keeps_stop_coverage_for_the_rest_and_does_not_record_a_close(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    r, profile, adapter = await open_one(w)
    t = first_trade(w)
    full = positions(w["sf"])[0].amount
    adapter.close_mode = "half"
    adapter.events.clear()
    await r._strategy_exit(profile, adapter, t, full, "x")
    left = positions(w["sf"])
    assert len(left) == 1 and left[0].amount == pytest.approx(full / 2)
    assert left[0].stop_price is not None                                 # the remaining half is still protected
    assert len(trades(w["sf"])) == 1                                      # NOT recorded as closed
    assert adapter.count("cancel_all") == 0


async def test_a_close_timeout_is_not_resent_and_a_close_that_did_fill_is_confirmed(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    r, profile, adapter = await open_one(w)
    t = first_trade(w)
    adapter.close_mode = "timeout_filled"
    adapter.events.clear()
    await r._strategy_exit(profile, adapter, t, 1.0, "x")
    assert adapter.count("market", reduce_only=True) == 1                 # never resent
    assert positions(w["sf"]) == [] and trades(w["sf"]) == []             # the re-read showed flat


async def test_a_close_timeout_that_closed_nothing_keeps_the_stop_and_the_record(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    r, profile, adapter = await open_one(w)
    t = first_trade(w)
    adapter.close_mode = "timeout_nofill"
    adapter.events.clear()
    await r._strategy_exit(profile, adapter, t, 1.0, "x")
    assert adapter.count("market", reduce_only=True) == 1
    assert len(positions(w["sf"])) == 1 and positions(w["sf"])[0].stop_price is not None
    assert len(trades(w["sf"])) == 1 and adapter.count("cancel_all") == 0


async def test_the_stop_filling_during_the_close_is_recognised_from_the_exchange_position(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    r, profile, adapter = await open_one(w)
    t = first_trade(w)
    adapter.close_mode = "stop_fills_first"
    await r._strategy_exit(profile, adapter, t, 1.0, "x")
    assert positions(w["sf"]) == [] and trades(w["sf"]) == []             # recorded once, after the exchange showed flat
    assert adapter.count("market", reduce_only=True) == 1


async def test_a_failed_close_leaves_the_stop_active_and_the_trade_open(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    r, profile, adapter = await open_one(w)
    t = first_trade(w)
    adapter.close_mode = "raise"
    adapter.events.clear()
    await r._strategy_exit(profile, adapter, t, 1.0, "x")
    pos = positions(w["sf"])
    assert len(pos) == 1 and pos[0].stop_price is not None
    assert len(trades(w["sf"])) == 1 and adapter.count("cancel_all") == 0
    assert "alice" not in r._unverified                                   # protection was confirmed, so entries are not blocked


async def test_a_failed_close_followed_by_a_failed_stop_reconciliation_blocks_entries(tmp_path, monkeypatch, caplog):
    w = build(tmp_path, monkeypatch)
    r, profile, adapter = await open_one(w)
    t = first_trade(w)
    adapter.close_mode = "raise"
    adapter.fail_stop_for = {A}
    with adapter.sf() as s:                                               # the exchange stop disappears meanwhile
        p = s.scalar(select(PaperPosition))
        p.stop_price = p.stop_order_id = None
        s.commit()
    with caplog.at_level(logging.INFO):
        await r._strategy_exit(profile, adapter, t, 1.0, "x")
    assert len(positions(w["sf"])) == 1 and len(trades(w["sf"])) == 1     # nothing is claimed closed
    assert "alice" in r._unverified and r._entries_blocked("alice")
    assert "NOT recorded as closed" in caplog.text or "not completed" in caplog.text


async def test_flatten_with_several_positions_keeps_every_stop_until_its_own_close_is_confirmed(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A, B, C))
    r, profile, adapter = await open_one(w, (A, B, C))
    adapter.close_modes = {B: "raise"}                                    # one position cannot be closed
    adapter.events.clear()
    r.controls.set(GLOBAL, flatten=True, reason="drill", by="admin")
    await r.risk_pass()
    left = {p.symbol: p for p in positions(w["sf"])}
    assert set(left) == {B} and left[B].stop_price is not None            # only the failing one remains, still protected
    assert {t.symbol for t in trades(w["sf"])} == {B}                     # the others were recorded closed, B was not
    for sym in (A, C):
        order = [e[0] for e in adapter.events if e[1] == sym and e[0] in ("market", "cancel_all")]
        assert order == ["market", "cancel_all"], f"{sym}: stop must outlive the close request, got {order}"
    assert adapter.count("cancel_all", B) == 0
    # next check retries, and once the close works everything is flat
    adapter.close_modes = {}
    await r.risk_pass()
    assert positions(w["sf"]) == [] and trades(w["sf"]) == []


async def test_a_market_order_that_returns_normally_but_leaves_the_position_open_is_never_recorded_closed(tmp_path, monkeypatch, caplog):
    w = build(tmp_path, monkeypatch)
    r, profile, adapter = await open_one(w)
    t = first_trade(w)
    adapter.close_mode = "noop"
    with caplog.at_level(logging.ERROR):
        await r._strategy_exit(profile, adapter, t, 1.0, "x")
    assert len(positions(w["sf"])) == 1 and len(trades(w["sf"])) == 1
    assert not [x for x in trade_log(w["sf"]) if x.exit_reason.startswith("strategy-exit")]
    assert positions(w["sf"])[0].stop_price is not None


async def test_on_closed_refuses_while_the_exchange_still_shows_a_position(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    r, profile, adapter = await open_one(w)
    t = first_trade(w)
    assert await r._on_closed(profile, adapter, t, reason="claimed") is False
    assert len(trades(w["sf"])) == 1 and trade_log(w["sf"]) == []


async def test_on_closed_refuses_when_protective_orders_cannot_be_verified_cancelled(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    r, profile, adapter = await open_one(w)
    t = first_trade(w)
    with adapter.sf() as s:                                               # the position is gone but a stop order lingers
        pos = s.scalar(select(PaperPosition))
        pos_stop = (pos.stop_price, pos.stop_order_id)
        s.delete(pos)
        s.commit()

    async def lingering(symbol):
        return [{"id": "stale", "stopPrice": 100.0, "side": "sell", "amount": 1.0, "reduceOnly": True}]

    adapter.fetch_stop_orders = lingering
    adapter.cancel_noop = True
    assert await r._on_closed(profile, adapter, t, reason="x") is False
    assert len(trades(w["sf"])) == 1                                      # record kept, cleanup retried later
    adapter.fetch_stop_orders = PaperExchange.fetch_stop_orders.__get__(adapter)
    adapter.cancel_noop = False
    assert await r._on_closed(profile, adapter, t, reason="x") is True
    assert trades(w["sf"]) == [] and pos_stop[0] is not None


async def test_an_unreadable_position_after_a_close_attempt_changes_nothing_and_blocks_entries(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    r, profile, adapter = await open_one(w)
    t = first_trade(w)

    async def down(symbols=None):
        raise ccxt.NetworkError("exchange unreachable")

    adapter.fetch_open_positions = down
    adapter.events.clear()
    await r._strategy_exit(profile, adapter, t, 1.0, "x")
    assert adapter.count("market") == 0 and adapter.count("cancel_all") == 0      # nothing was sent, nothing cancelled
    assert len(trades(w["sf"])) == 1 and "alice" in r._unverified
