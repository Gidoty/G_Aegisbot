"""Daily drawdown circuit breaker.

Measured on account equity (wallet balance + unrealized PnL) against the equity at the first
reading of the UTC day, normally the 00:00 UTC close. When the loss reaches the limit, new
entries stop until the next UTC day. Open positions are left alone: they keep their
exchange-side stop-loss orders.

Caveat: a deposit or withdrawal during the day shifts equity and can trip or mask the breaker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from g_aegisbot.config import DAILY_DRAWDOWN_LIMIT


def utc_date(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


def seconds_until_utc_midnight(now: datetime) -> int:
    now = now.astimezone(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds())


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    return f"{h}h {rem // 60}m"


def new_tracker(equity: float, now: datetime) -> dict:
    return {"date": utc_date(now), "start_equity": equity, "pnl": 0.0, "loss_pct": 0.0,
            "breaker_tripped": False, "tripped_at": None}


def update_tracker(
    tracker: dict | None, equity: float, now: datetime, limit: float = DAILY_DRAWDOWN_LIMIT
) -> tuple[dict, str | None]:
    """Return (new_tracker, event). Event is 'new_day', 'tripped' or None."""
    t = dict(tracker or {})
    event = None
    if t.get("date") != utc_date(now) or not t.get("start_equity"):
        t = new_tracker(equity, now)
        event = "new_day"
    start = t["start_equity"]
    t["pnl"] = equity - start
    t["loss_pct"] = max(0.0, -t["pnl"] / start) if start > 0 else 0.0
    if not t["breaker_tripped"] and start > 0 and -t["pnl"] >= limit * start:
        t["breaker_tripped"] = True
        t["tripped_at"] = now.astimezone(timezone.utc).isoformat()
        event = "tripped"
    return t, event


def trip_message(user_id: str, tracker: dict, equity: float, now: datetime, limit: float = DAILY_DRAWDOWN_LIMIT) -> str:
    start = tracker["start_equity"]
    return (
        f"[{user_id}] TRADING PAUSED until 00:00 UTC (in {fmt_duration(seconds_until_utc_midnight(now))}). "
        f"Your account is down {tracker['loss_pct']:.2%} today: equity is ${equity:,.2f} against "
        f"${start:,.2f} at the start of the UTC day, a loss of ${-tracker['pnl']:,.2f}. "
        f"The daily loss limit is {limit:.0%} (${start * limit:,.2f}). "
        "No new trades will open until the limit resets. Positions already open stay open and keep "
        "their stop-loss orders. Trading resumes automatically at the start of the next UTC day."
    )


def still_paused_message(user_id: str, tracker: dict, now: datetime) -> str:
    return (f"[{user_id}] Still paused by the daily loss limit (down {tracker['loss_pct']:.2%} today). "
            f"Entries resume at 00:00 UTC, in {fmt_duration(seconds_until_utc_midnight(now))}.")


def new_day_message(user_id: str, tracker: dict) -> str:
    return f"[{user_id}] New UTC day. Starting equity ${tracker['start_equity']:,.2f}; trading is enabled."
