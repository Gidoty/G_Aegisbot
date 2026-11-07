"""When may an unresolved entry order be forgotten? Only when the exchange shows a TERMINAL outcome for that exact order.
Elapsed time, any number of "not found" answers, and empty listings never release anything; errors never look like
empty results; a restart or a crash at any point cannot make an unresolved order disappear."""
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pytest
from sqlalchemy import create_engine, select

from g_aegisbot import audit
from g_aegisbot.db.trading_models import AuditLog, PaperPosition
from g_aegisbot.engine import journal as jr
from g_aegisbot.engine.runner import TradingRunner
from g_aegisbot.engine.strategy import StrategyParams
from g_aegisbot.exchange.binance_futures import EvidenceIncomplete
from tests.test_critical_safety import A, B, build, no_sleep, positions, prepared, trades


class Crash(BaseException):
    """The process dies here. A BaseException, so no `except Exception` in the bot can swallow it."""


async def pend(tmp_path, monkeypatch, symbols=(A,)):
    """A runner whose first entry timed out and whose order the exchange does not (yet) show. Returns (world, runner, adapter)."""
    w = build(tmp_path, monkeypatch, symbols)
    r, profile, adapter = await prepared(w)
    adapter.entry_mode = {A: "timeout_nofill"}
    adapter.lookup_answers = [None]
    await r.run_cycle()
    assert r._pending["alice"]
    adapter.entry_mode = {}
    return w, r, adapter


def advance(w, seconds):
    w["clock"].now += timedelta(seconds=seconds)


def journal(sf, action=None):
    with sf() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action.in_(("entry_intent", "entry_resolved")))))
    return [x for x in rows if action is None or x.action == action]


def new_runner(w):
    """A fresh process: a new TradingRunner on the same database and the same exchange."""
    return TradingRunner(w["sf"], w["runner"].box, StrategyParams(max_same_direction=0), market=w["market"],
                         clock=w["clock"], sleep=no_sleep)


def add_position(adapter, w, amount=5.0, stop=None):
    with adapter.sf() as s:
        s.add(PaperPosition(user_id="alice", symbol=A, side="buy", amount=amount, entry_price=120.0, leverage=3,
                            margin=200.0, entry_fee=0.0, opened_at=w["clock"].now, stop_price=stop,
                            stop_order_id="paper-stop-x" if stop else None,
                            next_check_ts=datetime(2100, 1, 1, tzinfo=timezone.utc)))
        s.commit()


async def passes(r, w, n, seconds=3600):
    for _ in range(n):
        advance(w, seconds)
        await r.risk_pass()


# ================================================================ no automatic release without a terminal outcome
async def test_hours_of_elapsed_time_never_release_even_when_every_source_is_clean(tmp_path, monkeypatch, caplog):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    await passes(r, w, 24)                                    # a whole day of risk passes, all sources answering "nothing"
    assert r._pending["alice"] and r._entries_blocked("alice")
    assert not journal(w["sf"], "entry_resolved")


async def test_any_number_of_not_found_answers_without_terminal_evidence_never_release(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    for _ in range(50):
        advance(w, 15)
        await r.risk_pass()
    assert r._pending["alice"] and adapter.lookup_calls >= 50     # it asked every time, and never concluded anything


async def test_an_empty_open_orders_listing_with_order_history_unavailable_never_releases(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    adapter.open_orders = []
    for failure in (ccxt.RequestTimeout("history timed out"), ccxt.ExchangeError("-1003"), EvidenceIncomplete("too many pages")):
        adapter.history = failure
        await passes(r, w, 3)
        assert r._pending["alice"]


async def test_each_missing_source_alone_blocks_a_not_found_order(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    for attr in ("trades", "open_orders", "conditional_orders", "history"):
        setattr(adapter, attr, ccxt.NetworkError("down"))
        await passes(r, w, 2)
        assert r._pending["alice"], attr
        setattr(adapter, attr, [])
    await passes(r, w, 2)
    assert r._pending["alice"], "all sources now clean, and it is STILL not released: only an operator can accept it"


async def test_trade_history_failure_keeps_even_a_terminal_unfilled_order_blocked(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    adapter.lookup_answers = [{"id": "77", "filled": 0, "status": "canceled"}]
    adapter.trades = ccxt.RequestTimeout("trades timed out")
    await passes(r, w, 3)
    assert r._pending["alice"]
    adapter.trades = EvidenceIncomplete("a history page did not advance")
    await passes(r, w, 1)
    assert r._pending["alice"]
    adapter.trades = []                                        # complete, nothing traded: now it is proven
    await passes(r, w, 1)
    assert not r._pending and "alice" not in r._unverified


async def test_a_canceled_zero_fill_status_that_the_trade_history_contradicts_stays_blocked(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    adapter.lookup_answers = [{"id": "77", "filled": 0, "status": "canceled"}]
    adapter.trades = [{"id": "t1", "order": "77", "side": "buy", "amount": 5.0}]     # a fill for the very same order
    await passes(r, w, 3)
    assert r._pending["alice"]


async def test_unexplained_fills_on_the_symbol_block_a_not_found_order(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    adapter.trades = [{"id": "t9", "order": "999", "side": "buy", "amount": 1.0}]
    await passes(r, w, 3)
    assert r._pending["alice"]


async def test_a_hedge_mode_position_never_releases(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    adapter.lookup_answers = [{"id": "77", "filled": 5.0, "status": "closed"}]
    add_position(adapter, w, stop=100.0)
    adapter.position_info = {"positionSide": "LONG"}           # one-way accounts report BOTH
    await passes(r, w, 3)
    assert r._pending["alice"]
    adapter.position_info = {"positionSide": "BOTH"}
    await passes(r, w, 1)
    assert not r._pending


@pytest.mark.parametrize("failure", [ccxt.RequestTimeout("timed out"), ccxt.ExchangeNotAvailable("503"),
                                     ccxt.RateLimitExceeded("429"), ccxt.ExchangeError("-1000 unknown"),
                                     ccxt.AuthenticationError("bad key")])
async def test_lookup_errors_and_timeouts_never_release(tmp_path, monkeypatch, failure):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    adapter.lookup_answers = [failure]
    await passes(r, w, 4)
    assert r._pending["alice"]
    adapter.lookup_answers = [{"id": "77", "filled": 0, "status": "canceled"}]       # when the exchange finally answers
    await passes(r, w, 1)
    assert not r._pending


async def test_an_unreadable_position_never_releases(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    adapter.lookup_answers = [{"id": "77", "filled": 0, "status": "canceled"}]
    real = adapter.fetch_open_positions

    async def down(symbols=None):
        raise ccxt.NetworkError("down")

    adapter.fetch_open_positions = down
    await passes(r, w, 3)
    assert r._pending["alice"]
    adapter.fetch_open_positions = real


async def test_an_order_still_working_is_cancelled_then_released_only_when_terminal(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    adapter.lookup_answers = [{"id": "77", "filled": 0, "status": "open"}]
    await passes(r, w, 3)
    assert r._pending["alice"] and adapter.cancel_by_id
    adapter.lookup_answers = [{"id": "77", "filled": 0, "status": "canceled"}]
    await passes(r, w, 1)
    assert not r._pending


async def test_an_adapter_that_cannot_look_orders_up_never_releases_a_flat_account(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    adapter.fetch_order_by_client_id = None
    await passes(r, w, 6)
    assert r._pending["alice"]


async def test_a_position_with_no_explanation_stays_blocked_while_it_is_protected(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    add_position(adapter, w)
    await passes(r, w, 3)
    assert r._pending["alice"], "no order is visible but a position exists: do not forget it"
    assert positions(w["sf"])[0].stop_price is not None


# ================================================================ automatic release: the only two ways
async def test_a_terminal_unfilled_order_with_a_flat_position_and_clean_trade_history_is_released(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    adapter.lookup_answers = [{"id": "77", "filled": 0, "status": "expired"}]
    await passes(r, w, 1)
    assert not r._pending and "alice" not in r._unverified
    assert "terminal with zero fill" in journal(w["sf"], "entry_resolved")[-1].detail


async def test_delayed_visibility_a_late_fill_is_protected_not_forgotten(tmp_path, monkeypatch):
    """The exchange first says 'no such order', then the order appears already filled."""
    w, r, adapter = await pend(tmp_path, monkeypatch)
    await passes(r, w, 2)
    assert r._pending["alice"]
    add_position(adapter, w)
    adapter.lookup_answers = [{"id": "77", "filled": 5.0, "status": "closed"}]
    await passes(r, w, 1)
    assert not r._pending and positions(w["sf"])[0].stop_price is not None      # released only because it is protected
    assert "terminal with a fill" in journal(w["sf"], "entry_resolved")[-1].detail


async def test_a_filled_order_is_not_released_until_its_position_is_confirmed_protected(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    add_position(adapter, w)
    adapter.lookup_answers = [{"id": "77", "filled": 5.0, "status": "closed"}]
    adapter.fail_stop_for = {A}
    adapter.close_modes = {A: "raise"}
    await r.risk_pass()
    assert r._pending["alice"] and r._entries_blocked("alice") and len(positions(w["sf"])) == 1
    adapter.fail_stop_for, adapter.close_modes = set(), {}
    await r.risk_pass()
    assert not r._pending and positions(w["sf"])[0].stop_price is not None


async def test_a_filled_order_whose_unprotectable_position_was_confirmed_closed_is_safe_to_release(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    add_position(adapter, w)
    adapter.lookup_answers = [{"id": "77", "filled": 5.0, "status": "closed"}]
    adapter.fail_stop_for = {A}
    await r.risk_pass()
    assert positions(w["sf"]) == [] and not r._pending


# ================================================================ journal durability and crash recovery
async def test_every_entry_is_journalled_before_it_is_sent_and_resolved_afterwards(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    seen = {}
    original = adapter.market_order

    async def spy(symbol, side, amount, reduce_only=False, **kw):
        if not reduce_only:
            seen["intents_before_send"] = [x.target for x in journal(w["sf"], "entry_intent")]
            seen["client_id"] = kw.get("client_id")
        return await original(symbol, side, amount, reduce_only, **kw)

    adapter.market_order = spy
    await r.run_cycle()
    assert seen["intents_before_send"] == [seen["client_id"]]                     # already committed when the order left
    assert [x.target for x in journal(w["sf"], "entry_resolved")] == [seen["client_id"]]
    assert journal(w["sf"], "entry_intent")[0].detail == A                        # the original intent is still there, unchanged


async def test_if_the_journal_cannot_be_written_nothing_is_sent_and_the_cycle_stops(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    attempts = []

    async def broken(uid, action, cid, detail):
        attempts.append(action)
        raise OSError("disk full")

    r._journal = broken
    await r.run_cycle()
    assert attempts == ["entry_intent"]
    assert adapter.count("market", reduce_only=False) == 0 and positions(w["sf"]) == []


async def test_a_client_order_id_is_used_once_ever(tmp_path, monkeypatch):
    from g_aegisbot.engine import strategy as strategy_mod
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    monkeypatch.setattr(strategy_mod, "new_client_order_id", lambda plan, now=None: "aeg-fixed-id")
    await r.run_cycle()
    assert adapter.count("market", reduce_only=False) == 1                        # the first used the id; the second was refused
    assert [x.target for x in journal(w["sf"], "entry_intent")] == ["aeg-fixed-id"]


async def test_generated_client_order_ids_do_not_collide(tmp_path, monkeypatch):
    from g_aegisbot.engine.strategy import TradePlan, new_client_order_id
    plan = TradePlan(symbol=A, side="buy", amount=1.0, entry_price=1.0, stop_price=0.9, leverage=3, margin=1.0,
                     notional=3.0, risk_usdt=0.1, conviction="normal", size_pct=0.05)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = {new_client_order_id(plan, now) for _ in range(5000)}                  # the same second, symbol and strategy
    assert len(ids) == 5000 and all(len(i) <= 36 for i in ids)


async def test_crash_after_the_intent_is_committed_but_before_the_order_is_sent(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    original = adapter.market_order

    async def die(symbol, side, amount, reduce_only=False, **kw):
        if not reduce_only:
            raise Crash()
        return await original(symbol, side, amount, reduce_only, **kw)

    adapter.market_order = die
    with pytest.raises(Crash):
        await r.run_cycle()
    adapter.market_order = original
    assert len(journal(w["sf"], "entry_intent")) == 1 and not journal(w["sf"], "entry_resolved")
    r2 = new_runner(w)
    await r2.start()
    await r2.run_cycle()
    assert r2._pending["alice"] and positions(w["sf"]) == []                      # blocked: nothing proves it never left
    await passes(r2, w, 12)
    assert r2._pending["alice"]


async def test_crash_after_the_order_filled_but_before_any_response_was_handled(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    original = adapter.market_order

    async def fill_then_die(symbol, side, amount, reduce_only=False, **kw):
        res = await original(symbol, side, amount, reduce_only, **kw)
        if not reduce_only:
            raise Crash()
        return res

    adapter.market_order = fill_then_die
    with pytest.raises(Crash):
        await r.run_cycle()
    adapter.market_order = original
    assert len(positions(w["sf"])) == 1 and positions(w["sf"])[0].stop_price is None      # filled, unprotected, unrecorded
    cid = journal(w["sf"], "entry_intent")[0].target
    adapter.lookup_answers = [{"id": "77", "clientOrderId": cid, "filled": 17.0, "status": "closed"}]
    adapter.events.clear()
    r2 = new_runner(w)
    await r2.start()
    await r2.run_cycle(allow_entries=False)                                       # the catch-up cycle after a restart
    pos = positions(w["sf"])
    assert len(pos) == 1 and pos[0].stop_price is not None and not r2._pending     # protected, then released
    assert adapter.count("market", reduce_only=False) == 0
    assert [x.target for x in journal(w["sf"], "entry_resolved")] == [cid]


async def test_crash_after_the_stop_was_placed_but_before_the_trade_was_recorded(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    r.store.open = lambda **kw: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        await r.run_cycle()
    assert len(positions(w["sf"])) == 1 and positions(w["sf"])[0].stop_price is not None and trades(w["sf"]) == []
    cid = journal(w["sf"], "entry_intent")[0].target
    adapter.lookup_answers = [{"id": "77", "clientOrderId": cid, "filled": 17.0, "status": "closed"}]
    r2 = new_runner(w)
    await r2.start()
    await r2.run_cycle(allow_entries=False)
    assert len(trades(w["sf"])) == 1 and not r2._pending                           # the existing stop was adopted


async def test_crash_after_the_response_but_before_the_journal_was_updated(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)

    async def dies_on_resolution(*a, **k):                  # the process dies before the resolution row is written
        raise Crash()

    r._resolve_journal = dies_on_resolution
    with pytest.raises(Crash):
        await r.run_cycle()
    assert len(positions(w["sf"])) == 1 and len(trades(w["sf"])) == 1               # everything else had completed
    assert len(journal(w["sf"], "entry_intent")) == 1 and not journal(w["sf"], "entry_resolved")
    cid = journal(w["sf"], "entry_intent")[0].target
    adapter.lookup_answers = [{"id": "77", "clientOrderId": cid, "filled": 17.0, "status": "closed"}]
    r2 = new_runner(w)
    await r2.start()
    await r2.risk_pass()
    assert not r2._pending
    assert [x.target for x in journal(w["sf"], "entry_resolved")] == [cid]          # now closed, exactly once


async def test_a_restart_cannot_forget_an_unresolved_order(tmp_path, monkeypatch):
    w, r1, adapter = await pend(tmp_path, monkeypatch)
    cid = next(iter(r1._pending["alice"]))
    adapter.lookup_answers = [{"id": "77", "filled": 0, "status": "open"}]          # still working on the exchange
    r2 = new_runner(w)
    await r2.start()
    await r2.run_cycle()
    assert set(r2._pending["alice"]) == {cid}
    assert r2._entries_blocked("alice") and adapter.count("market", reduce_only=False) == 1
    adapter.lookup_answers = [{"id": "77", "filled": 0, "status": "canceled"}]
    await r2.risk_pass()
    assert not r2._pending and [x.target for x in journal(w["sf"], "entry_resolved")] == [cid]
    r3 = new_runner(w)
    await r3.start()
    await r3.risk_pass()
    assert not r3._pending


async def test_restart_after_a_late_fill_protects_the_position_before_any_entry(tmp_path, monkeypatch):
    w, r1, adapter = await pend(tmp_path, monkeypatch, (A, B))
    add_position(adapter, w)
    adapter.lookup_answers = [{"id": "77", "filled": 5.0, "status": "closed"}]
    adapter.events.clear()
    r2 = new_runner(w)
    await r2.start()
    await r2.run_cycle(allow_entries=False)
    pos = [p for p in positions(w["sf"]) if p.symbol == A]
    assert len(pos) == 1 and pos[0].stop_price is not None and not r2._pending
    assert adapter.count("market", reduce_only=False) == 0 and any(t.symbol == A for t in trades(w["sf"]))


async def test_an_unresolved_intent_stays_blocking_however_old_it_is(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    audit.record(w["sf"], "alice", "entry_intent", "aeg-old-1", A, at=w["clock"].now - timedelta(days=45))
    await r.run_cycle()
    assert "aeg-old-1" in r._pending["alice"] and adapter.count("market", reduce_only=False) == 0
    await passes(r, w, 10)
    assert "aeg-old-1" in r._pending["alice"]                                       # evidence window would exceed 7 days: blocked


@pytest.mark.parametrize("target,detail", [("aeg-nosymbol", None), ("aeg-nosymbol", ""), (None, A), ("", A)])
async def test_a_damaged_journal_record_blocks_instead_of_being_skipped(tmp_path, monkeypatch, target, detail):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    audit.record(w["sf"], "alice", "entry_intent", target, detail, at=w["clock"].now)
    await r.run_cycle()
    assert r._pending["alice"] and all(pe.corrupt for pe in r._pending["alice"].values())
    assert adapter.count("market", reduce_only=False) == 0 and r._entries_blocked("alice")
    await passes(r, w, 5)
    assert r._pending["alice"]


async def test_a_journal_that_cannot_be_read_at_start_blocks_entries_and_is_retried(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    real = r._read_unresolved_intents
    calls = {"n": 0}

    def flaky(uid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("database locked")
        return real(uid)

    r._read_unresolved_intents = flaky
    await r.run_cycle()
    assert adapter.count("market", reduce_only=False) == 0 and "journal" in (r._entries_blocked("alice") or "")
    await r.run_cycle()
    assert len(positions(w["sf"])) == 1


async def test_resolved_orders_are_not_brought_back_after_a_restart(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    await r.run_cycle()
    assert len(positions(w["sf"])) == 1 and len(journal(w["sf"])) == 2
    r2 = new_runner(w)
    await r2.start()
    await r2.risk_pass()
    assert not r2._pending


# ================================================================ the audit table as a journal: structural guarantees
def test_nothing_in_the_codebase_deletes_or_rewrites_audit_rows():
    import ast
    import pathlib
    offenders = []
    for path in pathlib.Path("g_aegisbot").rglob("*.py"):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                args = " ".join(ast.dump(a) for a in node.args)
                if name in ("delete", "update") and "AuditLog" in args:
                    offenders.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "delete" and "AuditLog" in ast.dump(node.value):
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


def test_sqlite_commits_are_fully_synchronous(tmp_path):
    from g_aegisbot.db import get_engine
    eng = get_engine(f"sqlite:///{tmp_path}/j.db")
    with eng.connect() as c:
        assert c.exec_driver_sql("PRAGMA synchronous").scalar() == 2            # FULL: a committed intent survives power loss
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
