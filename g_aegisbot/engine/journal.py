"""The entry-order journal, kept in the audit table (append-only; nothing in the codebase deletes audit rows).

  entry_intent   written and committed BEFORE an entry order can be sent. actor = the user's profile, target = the client
                 order id, detail = the symbol.
  entry_resolved written when the outcome is established: by the runner (terminal exchange outcome) or by an operator
                 through resolve-entry. target = the same client order id; the writer is in `actor`. The detail starts with a
                 binding, `i=<intent row id> u=<user> s=<symbol> `, and only a resolution whose binding matches the original
                 intent (row, user and symbol) closes it. A row for the right client id but a different user, symbol or intent
                 closes nothing.

At most one resolution can ever be written for an intent: `resolve_once` is a single conditional INSERT (with a per-id
advisory lock on PostgreSQL), so two racing writers cannot both succeed.

An intent is open until a matching resolution exists. Rows are never updated or removed, so the
original intent always remains. There is no age limit: an old unresolved intent still blocks entries.
A damaged intent (no client order id or no symbol) is returned as `corrupt` and blocks entries until an operator deals with it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import DateTime, exists, insert, literal, select, text

from g_aegisbot.db.trading_models import AuditLog


@dataclass
class PendingEntry:
    """An entry order whose outcome is not yet established."""
    symbol: str
    client_id: str
    submitted_at: datetime
    detail: str
    corrupt: bool = False                    # the journal record is damaged; only an operator can resolve it
    row_id: int | None = None
    rejected: bool = False                   # the create call itself came back as an explicit exchange rejection


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_BINDING = re.compile(r"^i=(\d+) u=(\S+) s=(\S+) ")


def binding_of(detail: str | None) -> tuple[int, str, str] | None:
    """(intent row id, user, symbol) a resolution claims to resolve, or None if it carries no binding."""
    m = _BINDING.match(detail or "")
    return (int(m.group(1)), m.group(2), m.group(3)) if m else None


def resolution_detail(intent: AuditLog, symbol: str, body: str) -> str:
    return f"i={intent.id} u={intent.actor} s={symbol} {body}"[:400]


def resolves(resolution: AuditLog, intent: AuditLog) -> bool:
    """Does this resolution row close THIS intent? Same client id is not enough: the intent row, the user and the symbol must
    all match. (An intent whose own symbol is missing accepts the symbol the operator supplied.)"""
    b = binding_of(resolution.detail)
    if b is None or resolution.target != intent.target:
        return False
    return b[0] == intent.id and b[1] == intent.actor and (b[2] == intent.detail if intent.detail else bool(b[2]))


def read_unresolved(sf, uid: str | None = None) -> list[tuple[str, PendingEntry]]:
    """[(user, entry)] for every open intent (of one user, or of all), oldest first."""
    with sf() as s:
        q = select(AuditLog).where(AuditLog.action == "entry_intent").order_by(AuditLog.id)
        if uid is not None:
            q = q.where(AuditLog.actor == uid)
        intents = list(s.scalars(q))
        by_target: dict[str, list[AuditLog]] = {}
        for r in s.scalars(select(AuditLog).where(AuditLog.action == "entry_resolved")):
            if r.target:
                by_target.setdefault(r.target, []).append(r)
    out = []
    for r in intents:
        if r.target and any(resolves(x, r) for x in by_target.get(r.target, ())):
            continue
        if not r.target:
            pe = PendingEntry("?", f"journal-row-{r.id}", _aware(r.at), f"damaged journal record #{r.id}: no client order id",
                              corrupt=True, row_id=r.id)
        else:
            pe = PendingEntry(r.detail or "?", r.target, _aware(r.at),
                              "found unresolved in the order journal" if r.detail else "damaged journal record: no symbol",
                              corrupt=not r.detail, row_id=r.id)
        out.append((r.actor, pe))
    return out


def find_intent(sf, uid: str, client_id: str) -> AuditLog | None:
    with sf() as s:
        row = s.scalar(select(AuditLog).where(AuditLog.actor == uid, AuditLog.action == "entry_intent",
                                              AuditLog.target == client_id).order_by(AuditLog.id))
        if row is not None:
            s.expunge(row)
        return row


def find_resolution(sf, intent: AuditLog) -> AuditLog | None:
    """The resolution that closes this intent, or None. Rows for the same client id that do not bind to it are ignored."""
    with sf() as s:
        for row in s.scalars(select(AuditLog).where(AuditLog.action == "entry_resolved", AuditLog.target == intent.target)
                             .order_by(AuditLog.id)):
            if resolves(row, intent):
                s.expunge(row)
                return row
    return None


def state_of(sf, client_id: str, uid: str | None = None) -> tuple[bool, bool]:
    """(an intent row exists, THAT intent has been resolved). With `uid`, only that user's intent is considered."""
    with sf() as s:
        q = select(AuditLog).where(AuditLog.action == "entry_intent", AuditLog.target == client_id)
        if uid is not None:
            q = q.where(AuditLog.actor == uid)
        intents = list(s.scalars(q))
        resolutions = list(s.scalars(select(AuditLog).where(AuditLog.action == "entry_resolved", AuditLog.target == client_id)))
    resolved = bool(intents) and all(any(resolves(x, i) for x in resolutions) for i in intents)
    return bool(intents), resolved


def resolve_once(sf, intent: AuditLog, *, symbol: str, actor: str, body: str, at: datetime | None = None) -> bool:
    """Append the resolution of ONE intent, atomically and at most once. Returns True if this call wrote it, False if the
    intent already has a resolution (another writer won, or it was resolved earlier). Never updates or deletes anything.

    It is one INSERT ... SELECT ... WHERE NOT EXISTS statement, so the check and the write cannot be separated on SQLite
    (one writer at a time). On PostgreSQL a per-client-id advisory transaction lock serialises concurrent writers first,
    because two READ COMMITTED transactions could otherwise both pass the NOT EXISTS test."""
    prefix = f"i={intent.id} u={intent.actor} "
    detail = resolution_detail(intent, symbol, body)
    when = at or datetime.now(timezone.utc)
    with sf() as s:
        if s.get_bind().dialect.name == "postgresql":
            s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:cid, 0))"), {"cid": intent.target})
        already = exists().where(AuditLog.action == "entry_resolved", AuditLog.target == intent.target,
                                 AuditLog.detail.startswith(prefix, autoescape=True))
        stmt = insert(AuditLog).from_select(
            ["at", "actor", "action", "target", "detail"],
            select(literal(when, DateTime(timezone=True)), literal(actor[:64]), literal("entry_resolved"),
                   literal(intent.target), literal(detail)).where(~already))
        result = s.execute(stmt)
        s.commit()
        return result.rowcount == 1
