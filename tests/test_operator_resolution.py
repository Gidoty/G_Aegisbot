"""Operator resolution of a stuck entry order: pending-entries and resolve-entry.

The command always re-reads the exchange and shows what it found. It refuses when the evidence is incomplete,
contradictory or shows exposure, and a reason can never substitute for evidence. Even with complete evidence nothing changes
without --confirm and a reason. Resolutions are appended to the audit log; the original intent is never touched."""
import asyncio
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pytest
from sqlalchemy import select

from g_aegisbot import audit
from g_aegisbot.admin import Admin, run
from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.repository import ProfileRepository
from g_aegisbot.db.trading_models import AuditLog, PaperPosition
from g_aegisbot.exchange.binance_futures import EvidenceIncomplete
from tests.test_critical_safety import A, B, build, positions, prepared
from tests.test_pending_release import add_position, advance, journal, passes, pend

REASON = "checked order history, trades and positions in the Binance UI"


def operator(w, adapter, name="op-ana"):
    lines: list[str] = []
    admin = Admin(w["sf"], w["runner"].box, out=lines.append, actor=name, now=lambda: w["clock"].now,
                  adapter_factory=lambda profile: adapter)

    def sync_do(*argv):
        lines.clear()
        code = run(list(argv), admin)
        return code, "\n".join(lines)

    async def do(*argv):                      # the CLI uses asyncio.run, so it runs in a worker thread like a real shell would
        return await asyncio.to_thread(sync_do, *argv)

    return do


def cid_of(r):
    return next(iter(r._pending["alice"]))


async def resolve(do, cid, *extra, symbol=A, user="alice"):
    return await do("resolve-entry", user, cid, "--symbol", symbol, *extra)


def audit_rows(sf, action):
    with sf() as s:
        return list(s.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


# ================================================================ listing
async def test_pending_entries_lists_unresolved_orders_and_says_so_when_there_are_none(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    do = operator(w, adapter)
    code, out = await do("pending-entries")
    assert code == 0 and cid_of(r) in out and A in out and "alice" in out and "1 unresolved" in out
    adapter.lookup_answers = [{"id": "77", "filled": 0, "status": "canceled"}]
    await passes(r, w, 1)
    assert (await do("pending-entries"))[1] == "No unresolved entry orders."


# ================================================================ nothing changes without an explicit decision
async def test_a_dry_run_shows_the_evidence_and_changes_nothing(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    do = operator(w, adapter)
    before = len(audit.recent(w["sf"], 500))
    code, out = await resolve(do, cid_of(r))
    assert code == 0 and "Verdict: NOT_SEEN" in out and "Nothing was changed" in out
    for needle in ("order lookup:   NOT_FOUND", "order history:  COMPLETE", "account trades: COMPLETE", "open orders:    COMPLETE",
                   "conditional:    COMPLETE", "position:       FLAT", "evidence digest"):
        assert needle in out, needle
    assert len(audit.recent(w["sf"], 500)) == before and not journal(w["sf"], "entry_resolved")


@pytest.mark.parametrize("reason", [None, "", "ok", "too short"])
async def test_confirm_needs_a_real_reason(tmp_path, monkeypatch, reason):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    do = operator(w, adapter)
    args = ["--confirm"] + (["--reason", reason] if reason is not None else [])
    code, out = await resolve(do, cid_of(r), *args)
    assert code == 1 and "at least 12 characters" in out and not journal(w["sf"], "entry_resolved")


async def test_a_reason_without_confirm_changes_nothing(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    do = operator(w, adapter)
    code, out = await resolve(do, cid_of(r), "--reason", REASON)
    assert code == 0 and "Nothing was changed" in out and not journal(w["sf"], "entry_resolved")


# ================================================================ refusals: a reason can never substitute for evidence
REFUSALS = {
    "history error": lambda a, w: setattr(a, "history", ccxt.RequestTimeout("t")),
    "history incomplete": lambda a, w: setattr(a, "history", EvidenceIncomplete("too many pages")),
    "trades error": lambda a, w: setattr(a, "trades", ccxt.NetworkError("down")),
    "trades incomplete": lambda a, w: setattr(a, "trades", EvidenceIncomplete("window too long")),
    "unexplained fill": lambda a, w: setattr(a, "trades", [{"id": "t1", "order": "999", "side": "buy", "amount": 1.0}]),
    "open listing error": lambda a, w: setattr(a, "open_orders", ccxt.RequestTimeout("t")),
    "order open on the exchange": lambda a, w: setattr(a, "open_orders", [{"clientOrderId": "__CID__"}]),
    "conditional listing error": lambda a, w: setattr(a, "conditional_orders", ccxt.ExchangeError("unsupported")),
    "order listed as conditional": lambda a, w: setattr(a, "conditional_orders", [{"clientOrderId": "__CID__"}]),
    "order in history yet lookup said not found": lambda a, w: setattr(a, "history", [{"id": "5", "clientOrderId": "__CID__", "status": "FILLED"}]),
    "position exists": lambda a, w: add_position(a, w, stop=100.0),
    "lookup error": lambda a, w: setattr(a, "lookup_answers", [ccxt.RequestTimeout("t")]),
    "order still working": lambda a, w: setattr(a, "lookup_answers", [{"id": "5", "filled": 0, "status": "open"}]),
    "canceled but trades contradict it": lambda a, w: (setattr(a, "lookup_answers", [{"id": "5", "filled": 0, "status": "canceled"}]),
                                                      setattr(a, "trades", [{"id": "t1", "order": "5", "amount": 2.0}])),
    "canceled but a position exists": lambda a, w: (setattr(a, "lookup_answers", [{"id": "5", "filled": 0, "status": "canceled"}]),
                                                   add_position(a, w, stop=100.0)),
    "hedge-mode position": lambda a, w: (setattr(a, "position_info", {"positionSide": "SHORT"}), add_position(a, w, stop=100.0),
                                         setattr(a, "lookup_answers", [{"id": "5", "filled": 5.0, "status": "closed"}])),
}


@pytest.mark.parametrize("name", list(REFUSALS))
async def test_incomplete_or_contradictory_evidence_is_refused_even_with_confirm_and_a_good_reason(tmp_path, monkeypatch, name):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    cid = cid_of(r)
    sabotage = REFUSALS[name]
    sabotage(adapter, w)
    for attr in ("open_orders", "conditional_orders", "history"):                  # the placeholder means this order's own id
        v = getattr(adapter, attr)
        if isinstance(v, list):
            setattr(adapter, attr, [{**x, **({"clientOrderId": cid} if x.get("clientOrderId") == "__CID__" else {})} for x in v])
    do = operator(w, adapter)
    code, out = await resolve(do, cid, "--confirm", "--reason", REASON)
    assert code == 1 and "REFUSED" in out and "The block is unchanged" in out, out
    assert not journal(w["sf"], "entry_resolved")                                    # nothing was cleared
    refusal = audit_rows(w["sf"], "entry-resolve-refused")[-1]
    assert refusal.target == cid and refusal.actor == "op-ana" and "sha=" in refusal.detail
    await r.risk_pass()
    assert r._pending["alice"] and r._entries_blocked("alice")                       # and the running bot is still blocked


async def test_a_filled_order_with_an_unprotected_position_is_refused(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    add_position(adapter, w)                                                          # open, no stop
    adapter.lookup_answers = [{"id": "5", "filled": 5.0, "status": "closed"}]
    adapter.fail_stop_for = {A}                                                       # and the stop cannot be placed
    adapter.close_modes = {A: "raise"}
    do = operator(w, adapter)
    # the runner has been unable to protect it; the operator tool must not clear it either
    with adapter.sf() as s:
        p = s.scalar(select(PaperPosition)); p.stop_price = p.stop_order_id = None; s.commit()
    code, out = await resolve(do, cid_of(r), "--confirm", "--reason", REASON)
    assert code == 1 and "without a valid full-size protective stop" in out and not journal(w["sf"], "entry_resolved")


@pytest.mark.parametrize("args,needle", [
    (dict(symbol=B), "symbol mismatch"),
    (dict(cid="aeg-does-not-exist"), "has no entry order"),
    (dict(user="bob"), "No profile named"),
])
async def test_the_exact_intent_must_be_identified(tmp_path, monkeypatch, args, needle):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    do = operator(w, adapter)
    code, out = await resolve(do, args.get("cid", cid_of(r)), "--confirm", "--reason", REASON,
                        symbol=args.get("symbol", A), user=args.get("user", "alice"))
    assert code == 1 and needle in out and not journal(w["sf"], "entry_resolved")


async def test_another_users_intent_cannot_be_resolved_under_the_wrong_account(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    with w["sf"]() as s:
        ProfileRepository(s, w["runner"].box).create("bob", active_coins=[A])
    do = operator(w, adapter)
    code, out = await resolve(do, cid_of(r), "--confirm", "--reason", REASON, user="bob")
    assert code == 1 and "has no entry order" in out


# ================================================================ successful, audited resolution
async def test_a_complete_clean_reconciliation_is_resolved_only_with_confirm_and_a_reason_and_is_audited(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    cid = cid_of(r)
    intent_before = journal(w["sf"], "entry_intent")[0]
    snapshot = (intent_before.id, intent_before.at, intent_before.actor, intent_before.target, intent_before.detail)
    do = operator(w, adapter, name="op-ana")
    code, out = await resolve(do, cid, "--confirm", "--reason", REASON)
    assert code == 0 and "Resolved. Recorded as the operator 'op-ana'" in out
    row = journal(w["sf"], "entry_resolved")[0]
    assert row.actor == "op-ana" and row.target == cid and row.at is not None
    assert "operator op-ana" in row.detail and "not_seen" in row.detail and REASON[:40] in row.detail and "sha=" in row.detail
    assert "order=not_found" in row.detail and "position=flat" in row.detail
    kept = journal(w["sf"], "entry_intent")[0]
    assert (kept.id, kept.at, kept.actor, kept.target, kept.detail) == snapshot       # the original intent is untouched
    assert len(journal(w["sf"], "entry_intent")) == 1                                 # and still there
    # the running bot sees the resolution on its next check and entries are allowed again
    await r.risk_pass()
    assert not r._pending and "alice" not in r._unverified
    assert not r._entries_blocked("alice")
    # resolving it a second time is refused
    code, out = await resolve(do, cid, "--confirm", "--reason", REASON)
    assert code == 1 and "already resolved by op-ana" in out


async def test_a_restarted_bot_honours_the_operators_resolution(tmp_path, monkeypatch):
    from tests.test_pending_release import new_runner
    w, r, adapter = await pend(tmp_path, monkeypatch)
    cid = cid_of(r)
    code, _ = await resolve(operator(w, adapter), cid, "--confirm", "--reason", REASON)
    assert code == 0
    r2 = new_runner(w)
    await r2.start()
    await r2.risk_pass()
    assert not r2._pending and not r2._entries_blocked("alice")


async def test_a_filled_and_protected_order_may_be_resolved_by_the_operator(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    add_position(adapter, w, stop=100.0)
    adapter.lookup_answers = [{"id": "5", "filled": 5.0, "status": "closed"}]
    code, out = await resolve(operator(w, adapter), cid_of(r), "--confirm", "--reason", REASON)
    assert code == 0 and "Verdict: FILLED" in out and "filled" in journal(w["sf"], "entry_resolved")[0].detail


async def test_a_terminal_unfilled_order_with_clean_evidence_may_be_resolved_by_the_operator(tmp_path, monkeypatch):
    w, r, adapter = await pend(tmp_path, monkeypatch)
    adapter.lookup_answers = [{"id": "5", "filled": 0, "status": "canceled"}]
    code, out = await resolve(operator(w, adapter), cid_of(r), "--confirm", "--reason", REASON)
    assert code == 0 and "Verdict: UNFILLED" in out


async def test_a_damaged_record_without_a_symbol_can_be_resolved_only_with_the_symbol_and_clean_evidence(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, (A,))
    r, profile, adapter = await prepared(w)
    audit.record(w["sf"], "alice", "entry_intent", "aeg-nosymbol", None, at=w["clock"].now)
    await r.run_cycle()
    assert r._pending["alice"]["aeg-nosymbol"].corrupt
    do = operator(w, adapter)
    assert "DAMAGED" in (await do("pending-entries"))[1]
    code, out = await do("resolve-entry", "alice", "aeg-nosymbol", "--symbol", A, "--confirm", "--reason", REASON)
    assert code == 0, out
    await r.risk_pass()
    assert not r._pending


# ================================================================ operator-only, and secure
def test_the_api_module_cannot_reach_the_journal_or_reconciliation_code():
    import ast
    import inspect
    import g_aegisbot.api.app as app_module
    imported = set()
    for node in ast.walk(ast.parse(inspect.getsource(app_module))):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = ("g_aegisbot.admin", "g_aegisbot.engine.journal", "g_aegisbot.engine.reconcile", "g_aegisbot.engine.runner")
    assert not [m for m in imported if m.startswith(forbidden)]


def test_resolve_entry_output_and_audit_rows_contain_no_secrets(tmp_path):
    import io
    from sqlalchemy import create_engine
    from g_aegisbot.db import get_session_factory, init_db
    from g_aegisbot.security.crypto import SecretBox, generate_key
    engine = create_engine(f"sqlite:///{tmp_path}/s.db")
    init_db(engine)
    sf = get_session_factory(engine)
    box = SecretBox([generate_key()])
    lines: list[str] = []
    admin = Admin(sf, box, out=lines.append, stdin=io.StringIO("KEY123456\nSECRET987654\n"), actor="op")
    run(["create-user", "carol", "--mode", "testnet"], admin)
    audit.record(sf, "carol", "entry_intent", "aeg-paper-1", "BTC/USDT:USDT")
    run(["set-keys", "carol"], admin)
    code = run(["resolve-entry", "carol", "aeg-paper-1", "--symbol", "BTC/USDT:USDT", "--confirm", "--reason", REASON], admin)
    everything = "\n".join(lines) + " ".join(f"{x.actor} {x.action} {x.target} {x.detail}" for x in audit.recent(sf, 50))
    assert code == 0 and "KEY123456" not in everything and "SECRET987654" not in everything


def test_a_paper_profile_is_reconciled_against_its_own_ledger(tmp_path):
    from sqlalchemy import create_engine
    from g_aegisbot.db import get_session_factory, init_db
    from g_aegisbot.security.crypto import SecretBox, generate_key
    engine = create_engine(f"sqlite:///{tmp_path}/p.db")
    init_db(engine)
    sf = get_session_factory(engine)
    lines: list[str] = []
    admin = Admin(sf, SecretBox([generate_key()]), out=lines.append, actor="op")
    run(["create-user", "dora"], admin)                                   # testnet = paper ledger
    audit.record(sf, "dora", "entry_intent", "aeg-paper-2", "ETH/USDT:USDT")
    # a ledger position exists but has no stop: refused
    with sf() as s:
        s.add(PaperPosition(user_id="dora", symbol="ETH/USDT:USDT", side="buy", amount=1.0, entry_price=100.0, leverage=3,
                            margin=33.0, entry_fee=0.0, opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                            next_check_ts=datetime(2100, 1, 1, tzinfo=timezone.utc)))
        s.commit()
    lines.clear()
    code = run(["resolve-entry", "dora", "aeg-paper-2", "--symbol", "ETH/USDT:USDT", "--confirm", "--reason", REASON], admin)
    assert code == 1 and "protective stop" in "\n".join(lines)
    with sf() as s:
        p = s.scalar(select(PaperPosition)); p.stop_price, p.stop_order_id = 90.0, "paper-stop-1"; s.commit()
    lines.clear()
    assert run(["resolve-entry", "dora", "aeg-paper-2", "--symbol", "ETH/USDT:USDT", "--confirm", "--reason", REASON], admin) == 0
