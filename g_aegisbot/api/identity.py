"""Binding a Supabase login to a profile.

A profile is identified by the immutable Supabase user id (`auth_user_id`). The id is attached once, on the first login
of the person the profile was created for, and the email is never consulted again after that.

The attach is a single conditional UPDATE (`... WHERE auth_user_id IS NULL`) followed by a re-read, so two logins that
race for one profile cannot both win, and a profile that is already bound can never be taken over by presenting its email.
"""
from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from g_aegisbot.db.models import UserProfile


def find_unbound(sf, *, sub: str, email: str) -> tuple[UserProfile | None, str]:
    """The profile this login may claim, and how it matched ('legacy-id' or 'email'). Only profiles with no bound id
    are candidates. An empty email never matches anything."""
    with sf() as s:
        row = s.scalar(select(UserProfile).where(UserProfile.auth_user_id.is_(None),
                                                 func.lower(UserProfile.user_id) == sub.lower()))
        if row is not None:
            s.expunge(row)
            return row, "legacy-id"
        if not email:
            return None, ""
        row = s.scalar(select(UserProfile).where(
            UserProfile.auth_user_id.is_(None),
            (func.lower(UserProfile.user_id) == email) | (func.lower(UserProfile.login_email) == email),
        ).order_by(UserProfile.id))
        if row is not None:
            s.expunge(row)
        return row, ("email" if row is not None else "")


def claim_profile(sf, profile_id: int, sub: str, email: str = "") -> bool:
    """Attach `sub` to the profile if, and only if, nobody has attached one yet. True means THIS login now owns it
    (confirmed by re-reading the row). False means another login got there first, or the id is bound elsewhere."""
    with sf() as s:
        try:
            result = s.execute(
                update(UserProfile)
                .where(UserProfile.id == profile_id, UserProfile.auth_user_id.is_(None))
                .values(auth_user_id=sub, login_email=func.coalesce(UserProfile.login_email, email or None)))
            s.commit()
        except IntegrityError:                    # this Supabase id is already bound to a different profile
            s.rollback()
            return False
        if result.rowcount != 1:
            return False
    with sf() as s:
        return s.scalar(select(UserProfile.auth_user_id).where(UserProfile.id == profile_id)) == sub
