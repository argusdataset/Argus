"""Which date, and when — the arithmetic that keeps a scanner off the wall clock.

Every bug this file guards against has the same shape: the scanner decides
what to do from "now" instead of from the market calendar, and then a
holiday, a late run or a double-fire becomes a correctness problem.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from core.live_scanner.config import (
    ScannerConfig,
    ScannerSetting,
    ScannerSettings,
)
from core.live_scanner.schedule import (
    as_of_for,
    is_due,
    next_scan_time,
    pending_trading_days,
    scan_date_for,
    window_for,
    within_readiness_window,
)
from data.canonical_model.pit import session_close

#: A plain Tuesday, nowhere near a holiday.
TUESDAY = date(2026, 3, 3)
#: Independence Day 2026 falls on a Saturday, observed Friday the 3rd.
JULY_FRIDAY = date(2026, 7, 3)
#: Thanksgiving 2026.
THANKSGIVING = date(2026, 11, 26)


def test_the_cutoff_is_derived_from_the_session_close_not_from_now():
    """The property everything else rests on.

    A scan retried at 23:00 must see exactly what the 21:00 attempt would
    have, and a catch-up of last week must produce what that day produced.
    Both follow from `as_of` being a function of the date alone.
    """
    first = as_of_for(TUESDAY)
    second = as_of_for(TUESDAY)

    assert first == second
    assert first == session_close(TUESDAY) + timedelta(hours=17)


def test_the_offset_is_configuration_and_moving_it_moves_the_cutoff():
    late = ScannerConfig(
        settings=ScannerSettings(
            scan_offset_hours=ScannerSetting(value=21.0, kind="operational", rationale="test")
        )
    )

    assert as_of_for(TUESDAY, late) - as_of_for(TUESDAY) == timedelta(hours=4)


def test_a_scan_is_not_due_before_its_offset_has_elapsed():
    close = session_close(TUESDAY)

    assert not is_due(TUESDAY, close + timedelta(hours=1))
    assert is_due(TUESDAY, close + timedelta(hours=18))


def test_a_weekend_is_never_due():
    saturday = date(2026, 3, 7)

    assert not is_due(saturday, datetime(2026, 3, 10, tzinfo=UTC))


def test_a_market_holiday_is_never_due():
    """Module 07 already knows the calendar; this asserts the scanner asks it."""
    assert not is_due(THANKSGIVING, datetime(2026, 12, 1, tzinfo=UTC))
    assert not is_due(JULY_FRIDAY, datetime(2026, 7, 10, tzinfo=UTC))


def test_the_due_session_after_a_long_weekend_is_the_friday_before_it():
    """A scanner that assumed "yesterday" would look for a session that
    never happened and then have to decide what to do about it."""
    # The Monday after Thanksgiving week: Thursday was the holiday.
    friday_after = date(2026, 11, 27)
    monday = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)

    assert scan_date_for(monday) == friday_after


def test_nothing_is_due_on_a_tuesday_before_the_offset():
    """Tuesday's own session has not closed, and Monday's scan time has
    passed — so the answer is Monday, not None and not Tuesday.

    At the seventeen-hour offset, Monday's `due_at` lands at 14:00 UTC on
    Tuesday — not "Tuesday morning" any more, which is why this asks about
    mid-afternoon instead: early enough that Tuesday's own close (21:00
    UTC) has not happened, late enough that Monday's `due_at` already has.
    """
    tuesday_afternoon = datetime(2026, 3, 3, 16, 0, tzinfo=UTC)

    assert scan_date_for(tuesday_afternoon) == date(2026, 3, 2)


def test_the_due_date_is_todays_session_once_its_offset_has_elapsed():
    assert scan_date_for(as_of_for(TUESDAY) + timedelta(minutes=1)) == TUESDAY


def test_a_date_stays_worth_waiting_for_only_inside_the_readiness_window():
    """Past the window, missing data is not late — something is wrong, and
    continuing to record DATA_NOT_READY would be the scanner going quietly
    dark while looking busy."""
    close = session_close(TUESDAY)

    assert within_readiness_window(TUESDAY, close + timedelta(hours=20))
    assert not within_readiness_window(TUESDAY, close + timedelta(hours=25))


def test_asking_too_early_is_told_to_wait_rather_than_to_retry_immediately():
    close = session_close(TUESDAY)
    window = window_for(TUESDAY)

    assert next_scan_time(TUESDAY, close + timedelta(hours=1)) == window.due_at


def test_a_retry_time_is_offered_until_the_window_closes_and_then_is_none():
    close = session_close(TUESDAY)

    assert next_scan_time(TUESDAY, close + timedelta(hours=20)) is not None
    assert next_scan_time(TUESDAY, close + timedelta(hours=25)) is None


def test_pending_days_are_oldest_first_because_lifecycle_is_a_sequence():
    """Scanning Thursday before Wednesday would advance state machines out
    of order — an ACTIVATED event dated before the DETECTED that should
    have preceded it."""
    now = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)
    pending = pending_trading_days(now, completed=set())

    assert pending == sorted(pending)
    assert len(set(pending)) == len(pending)


def test_completed_dates_are_not_offered_again():
    now = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)
    everything = pending_trading_days(now, completed=set())
    done = set(everything[:3])

    remaining = pending_trading_days(now, completed=done)

    assert not (set(remaining) & done)
    assert len(remaining) == len(everything) - 3


def test_the_catchup_window_bounds_how_far_back_the_scanner_will_look():
    """The operational half of "incremental, never re-scans full history".

    The architecture makes any date scannable. This is what stops a
    scheduling mistake walking the scanner back through fifteen years.
    """
    now = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)
    narrow = ScannerConfig(
        settings=ScannerSettings(
            max_catchup_days=ScannerSetting(value=5.0, kind="operational", rationale="test")
        )
    )

    pending = pending_trading_days(now, completed=set(), config=narrow)

    assert len(pending) <= 5
    assert min(pending) >= date(2026, 3, 1)


def test_no_pending_day_is_offered_before_its_own_scan_time():
    """Today's session appears only once its offset has elapsed —
    otherwise catch-up would try to scan a day that is still trading."""
    just_after_close = session_close(TUESDAY) + timedelta(minutes=30)
    pending = pending_trading_days(just_after_close, completed=set())

    assert TUESDAY not in pending
    assert date(2026, 3, 2) in pending


def test_every_pending_day_is_a_trading_day():
    now = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
    pending = pending_trading_days(now, completed=set())

    assert THANKSGIVING not in pending
    assert all(day.weekday() < 5 for day in pending)


def test_a_scan_window_serializes_every_instant_it_computed():
    payload = window_for(TUESDAY).as_dict()

    assert payload["scan_date"] == TUESDAY.isoformat()
    assert payload["due_at"] > payload["session_close"]
    assert payload["expires_at"] > payload["due_at"]


@pytest.mark.parametrize("day", [date(2026, 3, 7), date(2026, 3, 8)])
def test_a_weekend_date_is_never_returned_as_the_due_session(day: date):
    assert scan_date_for(datetime.combine(day, datetime.min.time(), tzinfo=UTC)) != day
