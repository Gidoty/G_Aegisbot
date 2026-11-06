"""Kill switch state, stored in the database so the CLI, the API and the runner all see it.

  pause    : no new trades. Open positions keep their stops and keep being trailed.
  flatten  : pause, and the runner closes every position it manages (within about a minute) and
             cancels their stops. It stays in force until cleared, so nothing re-opens.
Scope is 'global' (all users) or one user_id. Either scope applies to a user.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from g_aegisbot.db.trading_models import Control

GLOBAL = "global"


@dataclass(frozen=True)
class ControlState:
    paused: bool = False
    flatten: bool = False
    reason: str | None = None
    scope: str | None = None

    def describe(self) -> str:
        who = "all accounts" if self.scope == GLOBAL else "this account"
        return f"{who}" + (f": {self.reason}" if self.reason else "")


class ControlStore:
    def __init__(self, session_factory):
        self.sf = session_factory

    def effective(self, user_id: str) -> ControlState:
        with self.sf() as s:
            rows = {r.scope: r for r in s.scalars(select(Control).where(Control.scope.in_([GLOBAL, user_id])))}
        active = [r for r in (rows.get(GLOBAL), rows.get(user_id)) if r and (r.paused or r.flatten)]
        if not active:
            return ControlState()
        flat = next((r for r in active if r.flatten), None)
        first = flat or active[0]
        return ControlState(paused=True, flatten=bool(flat), reason=first.reason, scope=first.scope)

    def set(self, scope: str, *, paused: bool = True, flatten: bool = False, reason: str | None = None,
            by: str | None = None) -> None:
        with self.sf() as s:
            row = s.get(Control, scope) or Control(scope=scope)
            row.paused, row.flatten = paused or flatten, flatten
            row.reason, row.set_by, row.set_at = (reason or "")[:200] or None, by, datetime.now(timezone.utc)
            s.merge(row)
            s.commit()

    def clear(self, scope: str) -> None:
        with self.sf() as s:
            row = s.get(Control, scope)
            if row:
                row.paused = row.flatten = False
                row.set_at = datetime.now(timezone.utc)
                s.commit()

    def active(self) -> list[Control]:
        with self.sf() as s:
            return [r for r in s.scalars(select(Control)) if r.paused or r.flatten]
