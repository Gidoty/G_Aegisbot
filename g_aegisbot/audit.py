from __future__ import annotations

from sqlalchemy import select

from g_aegisbot.db.trading_models import AuditLog


def record(session_factory, actor: str, action: str, target: str | None = None, detail: str | None = None) -> None:
    """Append an audit entry. Callers must never put keys, secrets or tokens in `detail`."""
    with session_factory() as s:
        s.add(AuditLog(actor=actor[:64], action=action[:48], target=(target or None) and target[:64],
                       detail=(detail or None) and detail[:400]))
        s.commit()


def recent(session_factory, limit: int = 20) -> list[AuditLog]:
    with session_factory() as s:
        return list(s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)))
