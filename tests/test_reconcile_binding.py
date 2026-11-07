"""Phase 1.3: entry-time reconciliation (M-1), stop visibility (M-3), bound and atomic resolutions (M-6), operator mode
check (M-8), identity of returned orders (M-12) and unreadable positions in the runner (H-3)."""
import threading
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pytest

from g_aegisbot import audit
from g_aegisbot.db.models import UserProfile  # noqa: F401  (keeps the models registered)
from g_aegisbot.engine import journal as jr
from g_aegisbot.engine.reconcile import decide, gather_evidence
from g_aegisbot.engine.strategy import ProtectionFailure, execute_plan
from g_aegisbot.exchange.binance_futures import PositionModeError
from tests.test_critical_safety import A, B, build, positions, prepared
from tests.test_execution_safety import Adapter, no_sleep, plan
from tests.test_operator_resolution import REASON, audit_rows, cid_of, operator, resolve
from tests.test_pending_release import journal, pend
from tests.test_reconcile_policy import CID, NOW, SYM, Adapter as FakeAdapter


# ---------------------------------------------------------------- M-1: the central policy applies at entry time
async def test_a_cancelled_entry_without_proof_blocks_the_next_signal(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    adapter.entry_mode = {A: "canceled"}
    adapter.lookup_answers = [None]                         # the exchange does not show the order: nothing is proven
    await r.run_cycle()
    assert positions(w["sf"]) == []                         # B was not opened behind an unproven A
    assert r._pending["alice"] and r._entries_blocked("alice")
    assert adapter.count("market", reduce_only=False) == 1


async def test_an_entry_timeout_with_a_failing_lookup_blocks_and_never_resends(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    adapter.entry_mode = {A: "timeout_nofill"}
    adapter.lookup_answers = [ccxt.RequestTimeout("lookup timed out")]
    await r.run_cycle()
    await r.run_cycle()
    assert adapter.count("market", reduce_only=False) == 1 and positions(w["sf"]) == []
    assert r._entries_blocked("alice")


async def test_an_entry_with_a_proven_cancel_is_released_by_the_same_policy(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    adapter.entry_mode = {A: "canceled"}
    adapter.lookup_answers = [{"id": "x", "status": "canceled", "filled": 0}]
    await r.run_cycle()
    assert [p.symbol for p in positions(w["sf"])] == [B] and not r._pending.get("alice")
    assert len(journal(w["sf"], "entry_resolved")) == len(journal(w["sf"], "entry_intent")) == 2     # A (proven) and B (filled)
    assert not r._entries_blocked("alice")


# ---------------------------------------------------------------- M-3: an accepted stop is not protection until it is visible
class InvisibleStop(Adapter):
    async def fetch_stop_orders(self, symbol):
        return []


class SmallStop(Adapter):
    async def fetch_stop_orders(self, symbol):
        return [{**s, "amount": s["amount"] / 2} for s in self.listed_stops]


@pytest.mark.parametrize("cls", [InvisibleStop, SmallStop])
async def test_a_stop_that_is_accepted_but_not_visible_or_too_small_is_a_protection_failure(cls):
    a = cls(entry={"filled": 1.0, "status": "closed"})
    with pytest.raises(ProtectionFailure) as exc:
        await execute_plan(a, plan(), sleep=no_sleep)
    assert "not confirmed" in exc.value.detail
    assert a.stop_calls                                    # the stop was placed; it simply could not be confirmed


async def test_a_visible_full_size_stop_is_confirmed():
    a = Adapter(entry={"filled": 1.0, "status": "closed"})
    out = await execute_plan(a, plan(), sleep=no_sleep)
    assert out["filled"] == 1.0


# ---------------------------------------------------------------- M-6: resolutions are bound, cross-user safe and at-most-once
async def test_a_resolution_for_another_user_or_intent_does_not_resolve_this_one(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    cid = cid_of(r)
    # a forged / foreign row with the same client id but another binding
    audit.record(w["sf"], "bob", "entry_resolved", cid, "i=999 u=bob s=" + A + " operator bob: x", NOW)
    audit.record(w["sf"], "bob", "entry_resolved", cid, "operator bob: unbound legacy-looking row", NOW)
    intent = jr.find_intent(w["sf"], "alice", cid)
    assert jr.find_resolution(w["sf"], intent) is None
    assert jr.find_intent(w["sf"], "bob", cid) is None
    assert jr.state_of(w["sf"], cid, "alice") == (True, False)
    await r.risk_pass()
    assert r._pending["alice"] and r._entries_blocked("alice")


async def test_a_resolution_is_written_at_most_once(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    intent = jr.find_intent(w["sf"], "alice", cid_of(r))
    assert jr.resolve_once(w["sf"], intent, symbol=A, actor="op-1", body="first") is True
    assert jr.resolve_once(w["sf"], intent, symbol=A, actor="op-2", body="second") is False
    rows = journal(w["sf"], "entry_resolved")
    assert len(rows) == 1 and rows[0].actor == "op-1"


async def test_concurrent_resolutions_write_exactly_one_row(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    intent = jr.find_intent(w["sf"], "alice", cid_of(r))
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        results.append(jr.resolve_once(w["sf"], intent, symbol=A, actor=f"op-{i}", body=f"racer {i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1 and len(journal(w["sf"], "entry_resolved")) == 1


async def test_the_operator_command_refuses_a_second_resolution_and_another_account(tmp_path, monkeypatch):
    from g_aegisbot.db.repository import ProfileRepository
    w, r, adapter = await pend(tmp_path, monkeypatch)
    cid = cid_of(r)
    with w["sf"]() as s:
        ProfileRepository(s, w["runner"].box).create("bob", active_coins=[A])
    do = operator(w, adapter)
    code, out = await resolve(do, cid, "--confirm", "--reason", REASON, user="bob")
    assert code == 1 and "has no entry order" in out and not journal(w["sf"], "entry_resolved")
    code, _ = await resolve(do, cid, "--confirm", "--reason", REASON)
    assert code == 0 and len(journal(w["sf"], "entry_resolved")) == 1
    code, out = await resolve(do, cid, "--confirm", "--reason", REASON)
    assert code == 1 and "already resolved" in out and len(journal(w["sf"], "entry_resolved")) == 1


# ---------------------------------------------------------------- M-8: operator recovery checks the account position mode
async def test_operator_recovery_is_refused_when_the_account_is_in_hedge_mode(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    cid = cid_of(r)

    async def hedge():
        raise PositionModeError("hedge mode is on")

    adapter.assert_one_way_mode = hedge
    do = operator(w, adapter)
    code, out = await resolve(do, cid, "--confirm", "--reason", REASON)
    assert code == 1 and "REFUSED" in out and "HEDGE" in out.upper() and not journal(w["sf"], "entry_resolved")


async def test_operator_recovery_is_refused_when_the_mode_cannot_be_read(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)

    async def unreadable():
        raise ccxt.NetworkError("down")

    adapter.assert_one_way_mode = unreadable
    code, out = await resolve(operator(w, adapter), cid_of(r), "--confirm", "--reason", REASON)
    assert code == 1 and "REFUSED" in out and not journal(w["sf"], "entry_resolved")


# ---------------------------------------------------------------- M-12: a returned order must be the order that was asked for
async def _gather(lookup):
    clean = dict(lookup=lookup, history=[], trades=[], open=[], conditional=[], positions=[], stops=[])
    return await gather_evidence(_Raw(**clean), SYM, CID, NOW, NOW + timedelta(minutes=5))


class _Raw(FakeAdapter):
    async def fetch_order_by_client_id(self, symbol, cid):
        return self._get("lookup")                          # returned exactly as the exchange sent it: no identity added


@pytest.mark.parametrize("answer", [
    {"id": "1", "status": "closed", "filled": 1.0, "clientOrderId": "someone-else", "symbol": SYM},
    {"id": "1", "status": "closed", "filled": 1.0, "clientOrderId": CID, "symbol": "ETH/USDT:USDT"},
    {"id": "1", "status": "closed", "filled": 1.0, "symbol": SYM},
    {"id": "1", "status": "closed", "filled": 1.0, "clientOrderId": CID},
    {"id": "1", "status": "closed", "filled": 1.0, "clientOrderId": CID, "symbol": SYM, "info": {"clientOrderId": "other"}},
])
async def test_an_order_with_the_wrong_client_id_or_symbol_never_counts_as_this_order(answer):
    e = await _gather(answer)
    assert e.lookup_state == "error" and decide(e).outcome == "blocked"


async def test_an_order_with_the_right_identity_is_accepted():
    e = await _gather({"id": "1", "status": "canceled", "filled": 0, "clientOrderId": CID, "symbol": SYM})
    assert e.lookup_state == "found"


# ---------------------------------------------------------------- H-3 in the runner: an unreadable position is never flat
@pytest.mark.parametrize("row", [
    {"symbol": A, "side": "long", "contracts": float("nan")},
    {"symbol": A, "side": "long", "contracts": float("inf")},
    {"symbol": A, "side": "long", "contracts": None},
    {"symbol": A, "side": "long", "contracts": "abc"},
    {"symbol": A, "side": "long"},
])
async def test_the_runner_treats_a_malformed_position_as_unreadable(tmp_path, monkeypatch, row):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)

    async def broken(symbols=None):
        return [row]

    adapter.fetch_open_positions = broken
    readable, qty, side = await r._position_state(adapter, A)
    assert readable is False and qty == 0.0
