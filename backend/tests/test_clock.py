from datetime import datetime, timezone

from app.clock import FakeClock


def test_fake_clock_is_controllable():
    start = datetime(2026, 9, 30, tzinfo=timezone.utc)
    clock = FakeClock(start)
    assert clock.now() == start
    later = datetime(2026, 10, 2, tzinfo=timezone.utc)
    clock.set(later)
    assert clock.now() == later
