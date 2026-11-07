"""Rules shared by the admin CLI and the API, so both enforce the same thing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from g_aegisbot.db.models import UserProfile


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def extend_subscription(profile: UserProfile, days: int, now: datetime) -> datetime:
    """Add days on top of any remaining time (or from now if expired) and activate."""
    if days <= 0:
        raise ValueError("days must be positive")
    current = as_utc(profile.subscription_expires_at)
    base = max(now, current) if current else now
    profile.subscription_status = "active"
    profile.subscription_expires_at = base + timedelta(days=days)
    return profile.subscription_expires_at


KEYS_MAX_AGE = timedelta(days=7)
KEYS_MISSING = "API key verification missing"
KEYS_FUTURE = "API key verification is dated in the future"
KEYS_STALE = "API key verification is older than 7 days"
KEYS_UNREADABLE = "API key verification record is unreadable"


def keys_verification_problem(profile: UserProfile, now: datetime | None = None) -> str | None:
    """Why this profile's API keys do not count as verified right now, or None if they do. Fails closed: a missing,
    unreadable, future-dated or stale timestamp is a problem. (SQLite returns naive datetimes; they are read as UTC.)"""
    now = as_utc(now) if now is not None else datetime.now(timezone.utc)
    try:
        stamp = getattr(profile, "keys_verified_at", None)
        if stamp is None:
            return KEYS_MISSING
        if not isinstance(stamp, datetime):
            return KEYS_UNREADABLE
        stamp = as_utc(stamp)
        if stamp > now:
            return KEYS_FUTURE
        if now - stamp > KEYS_MAX_AGE:
            return KEYS_STALE
    except Exception:
        return KEYS_UNREADABLE
    return None


def live_switch_problems(profile: UserProfile, confirmed: bool, now: datetime | None = None) -> list[str]:
    """What stops this profile from trading real money. Empty means it may go live."""
    problems = []
    if not profile.has_credentials:
        problems.append("no API keys stored")
    keys = keys_verification_problem(profile, now)
    if keys:
        problems.append(keys)
    if float(profile.max_wallet_cap_usdt or 0) <= 0:
        problems.append("wallet cap is 0")
    if not confirmed:
        problems.append("real-money confirmation missing")
    return problems
