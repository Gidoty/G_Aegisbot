from datetime import datetime, timezone

from g_aegisbot.engine import risk

T = lambda d, h=12, m=0: datetime(2026, 3, d, h, m, tzinfo=timezone.utc)


def test_first_reading_starts_the_day():
    t, ev = risk.update_tracker(None, 10_000, T(5))
    assert ev == "new_day" and t["start_equity"] == 10_000 and not t["breaker_tripped"]


def test_trips_at_exactly_four_percent_not_before():
    t, _ = risk.update_tracker(None, 10_000, T(5))
    t, ev = risk.update_tracker(t, 9_600.01, T(5, 13))
    assert ev is None and not t["breaker_tripped"] and t["loss_pct"] < 0.04
    t, ev = risk.update_tracker(t, 9_600.0, T(5, 14))
    assert ev == "tripped" and t["breaker_tripped"] and t["tripped_at"].startswith("2026-03-05T14")


def test_stays_tripped_even_if_equity_recovers_same_day():
    t, _ = risk.update_tracker(None, 10_000, T(5))
    t, _ = risk.update_tracker(t, 9_500, T(5, 13))
    t, ev = risk.update_tracker(t, 10_200, T(5, 20))
    assert t["breaker_tripped"] and ev is None


def test_resets_at_next_utc_day_with_fresh_baseline():
    t, _ = risk.update_tracker(None, 10_000, T(5))
    t, _ = risk.update_tracker(t, 9_500, T(5, 13))
    t, ev = risk.update_tracker(t, 9_500, T(6, 0, 0))
    assert ev == "new_day" and not t["breaker_tripped"] and t["start_equity"] == 9_500 and t["pnl"] == 0


def test_profit_never_trips():
    t, _ = risk.update_tracker(None, 10_000, T(5))
    t, ev = risk.update_tracker(t, 12_000, T(5, 15))
    assert not t["breaker_tripped"] and t["loss_pct"] == 0.0


def test_countdown_and_messages():
    assert risk.seconds_until_utc_midnight(T(5, 18, 30)) == 5.5 * 3600
    assert risk.fmt_duration(5.5 * 3600) == "5h 30m"
    t, _ = risk.update_tracker(None, 10_000, T(5))
    t, _ = risk.update_tracker(t, 9_588, T(5, 18, 30))
    msg = risk.trip_message("alice", t, 9_588, T(5, 18, 30))
    for needle in ("alice", "PAUSED", "00:00 UTC", "5h 30m", "4.12%", "$412.00", "$400.00", "stop-loss", "resumes automatically"):
        assert needle in msg
