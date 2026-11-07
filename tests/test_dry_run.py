"""--dry-run must be genuinely non-mutating: read-only exchange queries are fine, but nothing may be sent, cancelled, moved,
closed, resolved or written to the journal. Reconciliation still runs and still REPORTS what it found."""
import logging
from datetime import timedelta

import pytest
from sqlalchemy import select

from g_aegisbot import audit
from g_aegisbot.db.trading_models import AuditLog, PaperPosition, TradeLog
from tests.test_critical_safety import A, B, build, positions, prepared, trades
from tests.test_pending_release import add_position, journal
from tests.test_protection_checks import Book, stop

MUTATORS = ("market_order", "stop_market", "cancel_order", "cancel_all", "cancel_order_by_client_id", "process_stops",
            "configure_symbol")


def watch(adapter) -> list[str]:
    """Record every call to a method that can change exchange (or paper-ledger) state."""
    calls: list[str] = []
    for name in MUTATORS:
        real = getattr(adapter, name)

        async def wrapper(*a, _n=name, _r=real, **k):
            calls.append(_n)
            return await _r(*a, **k)

        setattr(adapter, name, wrapper)
    return calls


def audit_rows(sf):
    with sf() as s:
        return [(r.id, r.actor, r.action, r.target, r.detail) for r in s.scalars(select(AuditLog).order_by(AuditLog.id))]


async def test_dry_run_with_a_working_entry_signal_sends_and_writes_nothing(tmp_path, monkeypatch, caplog):
    w = build(tmp_path, monkeypatch, (A, B))
    r, profile, adapter = await prepared(w)
    r.dry_run = True
    calls = watch(adapter)
    before = audit_rows(w["sf"])
    with caplog.at_level(logging.INFO):
        await r.run_cycle()
        await r.risk_pass()
    assert calls == [] and positions(w["sf"]) == [] and trades(w["sf"]) == []
    assert audit_rows(w["sf"]) == before                                    # no entry_intent either
    assert caplog.text.count("DRY RUN, would open") == 2                    # and it still reported what it would have done


@pytest.mark.parametrize("lookup,expect_log", [
    ({"id": "77", "filled": 0, "status": "open"}, "WOULD be cancelled"),
    ({"id": "77", "filled": 0, "status": "canceled"}, "WOULD be reconciled"),
    ({"id": "77", "filled": 5.0, "status": "closed"}, "WOULD be reconciled"),
])
async def test_dry_run_with_an_unresolved_entry_reports_it_but_resolves_and_cancels_nothing(tmp_path, monkeypatch, caplog, lookup, expect_log):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    audit.record(w["sf"], "alice", "entry_intent", "aeg-pending-1", A, at=w["clock"].now - timedelta(minutes=10))
    if lookup["status"] == "closed":
        add_position(adapter, w, stop=100.0)
    adapter.lookup_answers = [lookup]
    r.dry_run = True
    calls = watch(adapter)
    before = audit_rows(w["sf"])
    with caplog.at_level(logging.INFO):
        await r.run_cycle(allow_entries=False)
        await r.risk_pass()
    assert adapter.lookup_calls >= 2, "reconciliation must still READ the exchange in dry-run"
    assert expect_log in caplog.text
    assert "aeg-pending-1" in r._pending["alice"]                           # still pending: nothing was released
    assert audit_rows(w["sf"]) == before and not journal(w["sf"], "entry_resolved")
    assert not set(calls) & {"cancel_order", "cancel_all", "cancel_order_by_client_id", "market_order", "stop_market"}
    assert adapter.cancel_by_id == []


async def test_dry_run_does_not_cancel_the_stops_of_a_trade_whose_position_has_gone(tmp_path, monkeypatch, caplog):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    await r.run_cycle()                                                     # a real (paper) trade with a stop
    assert len(trades(w["sf"])) == 1
    with adapter.sf() as s:                                                 # the stop filled: the position is gone
        pos = s.scalar(select(PaperPosition))
        s.delete(pos)
        s.commit()
    r.dry_run = True
    calls = watch(adapter)
    with caplog.at_level(logging.INFO):
        await r.run_cycle(allow_entries=False)
    assert "cancel_all" not in calls and "cancel_order" not in calls
    assert len(trades(w["sf"])) == 1                                        # the record was not removed
    with w["sf"]() as s:
        assert list(s.scalars(select(TradeLog))) == []                      # and no close was logged
    assert "DRY RUN" in caplog.text and "WOULD be recorded as closed" in caplog.text


async def test_dry_run_does_not_sync_the_stop_record_to_the_exchange(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    await r.run_cycle()
    from g_aegisbot.db.trading_models import OpenTrade
    with w["sf"]() as s:
        t = s.scalar(select(OpenTrade)); t.stop_order_id = "stale-id"; s.commit()
    r.dry_run = True
    await r.risk_pass()
    with w["sf"]() as s:
        assert s.scalar(select(OpenTrade)).stop_order_id == "stale-id"      # untouched in dry-run
    r.dry_run = False
    await r.risk_pass()
    with w["sf"]() as s:
        assert s.scalar(select(OpenTrade)).stop_order_id != "stale-id"      # and corrected when it is a real run


async def test_dry_run_places_no_stop_and_closes_nothing_for_an_unprotected_position(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    add_position(adapter, w)                                                # open, no stop, no record
    r.dry_run = True
    calls = watch(adapter)
    await r.risk_pass()
    await r.run_cycle(allow_entries=False)
    assert not set(calls) & {"stop_market", "market_order", "cancel_all", "cancel_order"}
    assert positions(w["sf"])[0].stop_price is None and trades(w["sf"]) == []


async def test_dry_run_does_not_run_the_paper_stop_simulation_but_a_real_run_does(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    calls = watch(adapter)
    r.dry_run = True
    await r.risk_pass()
    assert "process_stops" not in calls
    r.dry_run = False
    await r.risk_pass()
    assert "process_stops" in calls


async def test_dry_run_leaves_surplus_stops_alone(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    book = Book({A: ("long", 10.0, 100.0)}, {A: [stop("loose", price=94.0), stop("tight", price=97.0)]})
    r.dry_run = True
    await r._ensure_protected(profile, book)
    assert [e for e in book.events if e[0] in ("cancel", "cancel_all", "stop_market", "market")] == []
    assert [o["id"] for o in book.stops[A]] == ["loose", "tight"]


async def test_dry_run_does_not_cancel_a_cleanup_debt(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    r._cleanup_debt["alice"].add(A)
    r.dry_run = True
    calls = watch(adapter)
    await r._settle_cleanup(profile, adapter)
    assert calls == [] and A in r._cleanup_debt["alice"]


# ---------------------------------------------------------------- failed cancels (C-2) at runner level
class StuckCancel(Book):
    """Cancels are accepted by the exchange API call but the order stays listed (or the call itself fails)."""

    async def cancel_order(self, symbol, order_id, **kw):
        self.events.append(("cancel", symbol, order_id))
        raise RuntimeError("cancel not confirmed")


async def test_a_surplus_stop_that_cannot_be_proven_cancelled_leaves_the_account_unverified(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    book = StuckCancel({A: ("long", 10.0, 100.0)}, {A: [stop("loose", price=94.0), stop("tight", price=97.0)]})
    from g_aegisbot.engine.protection import ProtectionError
    with pytest.raises(ProtectionError, match="could not prove surplus stop"):      # the caller marks the account unverified
        await r._ensure_protected(profile, book)
    assert ("cancel", A, "loose") in book.events                           # it tried
    assert [o["id"] for o in book.stops[A]] == ["loose", "tight"]          # nothing is assumed gone


async def test_a_failed_cancel_of_the_old_stop_after_a_trail_marks_the_account_unverified(tmp_path, monkeypatch):
    from types import SimpleNamespace
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    book = StuckCancel({A: ("long", 10.0, 100.0)}, {A: [stop("old", price=94.0)]})
    t = SimpleNamespace(symbol=A, side="buy", stop_price=94.0, stop_order_id="old")
    new_id = await r._move_stop(profile, book, t, 97.0, 10.0)
    assert new_id and "alice" in r._unverified and "old" in r._unverified["alice"]
    assert r._entries_blocked("alice")
