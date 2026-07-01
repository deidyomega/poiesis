from __future__ import annotations

from datetime import datetime, timezone

from poiesis.scheduler import is_due


def _daily(hour=10, minute=0, tz="UTC", last_run=None):
    return {"kind": "daily", "at_hour": hour, "at_minute": minute, "tz": tz, "last_run": last_run}


def _interval(seconds, last_run=None):
    return {"kind": "interval", "interval_seconds": seconds, "last_run": last_run}


def test_daily_fires_after_target_when_never_run():
    now = datetime(2026, 6, 30, 11, 0, tzinfo=timezone.utc)
    assert is_due(_daily(10), now)


def test_daily_not_due_before_target():
    now = datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc)
    assert not is_due(_daily(10), now)


def test_daily_not_due_if_already_ran_today():
    now = datetime(2026, 6, 30, 11, 0, tzinfo=timezone.utc)
    ran = "2026-06-30T10:05:00+00:00"
    assert not is_due(_daily(10, last_run=ran), now)


def test_daily_due_again_next_day():
    now = datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc)
    ran = "2026-06-30T10:05:00+00:00"
    assert is_due(_daily(10, last_run=ran), now)


def test_daily_respects_timezone():
    # 10:00 in New York == 14:00 UTC (EDT). At 13:00 UTC it's 09:00 local -> not due.
    now = datetime(2026, 6, 30, 13, 0, tzinfo=timezone.utc)
    assert not is_due(_daily(10, tz="America/New_York"), now)
    # At 15:00 UTC it's 11:00 local -> due.
    now2 = datetime(2026, 6, 30, 15, 0, tzinfo=timezone.utc)
    assert is_due(_daily(10, tz="America/New_York"), now2)


def test_interval():
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    assert is_due(_interval(3600), now)  # never run
    assert is_due(_interval(3600, last_run="2026-06-30T10:00:00+00:00"), now)  # 2h ago
    assert not is_due(_interval(3600, last_run="2026-06-30T11:30:00+00:00"), now)  # 30m ago
