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


def live_switch_problems(profile: UserProfile, confirmed: bool) -> list[str]:
    """What stops this profile from trading real money. Empty means it may go live."""
    problems = []
    if not profile.has_credentials:
        problems.append("no API keys stored")
    if float(profile.max_wallet_cap_usdt or 0) <= 0:
        problems.append("wallet cap is 0")
    if not confirmed:
        problems.append("real-money confirmation missing")
    return problems
