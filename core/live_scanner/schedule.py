"""When to scan, and for which date — the market calendar, not the clock.

## The date being scanned is not the date it is

A scan of Tuesday's session runs on Tuesday evening, and every read inside
it is bounded by an `as_of` derived from Tuesday's 16:00 ET close plus an
offset — never by "now". That separation is what makes a scan reproducible
and what makes a catch-up run of last Thursday produce exactly what
Thursday's scan would have: the same call, the same cutoff, the same
answer. It is Module 07's dual-mode discipline, applied to the one place
where the temptation to reach for a wall clock is strongest.

So nothing here returns "today". `scan_date_for(now)` answers "which
trading session is the most recent one that should have been scanned by
this instant", which is a different question with a different answer on a
Saturday, on a public holiday, and at 5pm on a Tuesday.

## Non-trading days are not scans that were skipped

They are dates that were never scan dates. Module 07's `is_trading_day`
already knows the US equity calendar including holidays, so this module
asks it rather than modelling weekends itself. No row is written for a
Saturday — see `infra/db/schema/live_scanner.py` on why recording one
would make the table mostly noise.

## The readiness window

`due_at` is when the scanner should first ask about a date. It is *not* a
deadline: `within_readiness_window` says whether a date is still worth
waiting for, and past that window a scan that still has no data is not
late, it is broken — which is `readiness.py`'s distinction to draw and
this module's to bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from core.data_validation.calendar import is_trading_day
from core.live_scanner.config import ScannerConfig
from data.canonical_model.pit import session_close

__all__ = [
    "ScanWindow",
    "as_of_for",
    "due_at",
    "is_due",
    "next_scan_time",
    "pending_trading_days",
    "scan_date_for",
    "within_readiness_window",
]


@dataclass(frozen=True, slots=True)
class ScanWindow:
    """One scan date's timing: when it closed, when to ask, when to stop."""

    scan_date: date
    session_close: datetime
    due_at: datetime
    expires_at: datetime

    def as_dict(self) -> dict[str, str]:
        return {
            "scan_date": self.scan_date.isoformat(),
            "session_close": self.session_close.isoformat(),
            "due_at": self.due_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


def as_of_for(scan_date: date, config: ScannerConfig | None = None) -> datetime:
    """The PIT cutoff a scan of `scan_date` uses.

    Derived from the session close, never from the current time. Two
    consequences, both load-bearing: a scan retried at 23:00 sees exactly
    what the 21:00 attempt would have, and a catch-up run of a date from
    last week produces what that day's scan would have produced rather
    than what today's data says about it.
    """
    config = config or ScannerConfig()
    return session_close(scan_date) + config.settings.scan_offset


def due_at(scan_date: date, config: ScannerConfig | None = None) -> datetime:
    """When the scanner should first ask whether this date is scannable.

    The same instant as the PIT cutoff, and deliberately so: asking
    earlier than the cutoff would be asking about data the scan would then
    refuse to look at.
    """
    return as_of_for(scan_date, config)


def window_for(scan_date: date, config: ScannerConfig | None = None) -> ScanWindow:
    config = config or ScannerConfig()
    close = session_close(scan_date)
    return ScanWindow(
        scan_date=scan_date,
        session_close=close,
        due_at=as_of_for(scan_date, config),
        expires_at=close + config.settings.readiness_window,
    )


def scan_date_for(now: datetime, config: ScannerConfig | None = None) -> date | None:
    """The most recent trading session that is due to have been scanned.

    None when nothing is due yet — which happens before the first
    scheduled time on a Monday morning, and all weekend if the previous
    Friday has already been handled.

    Walks backwards from `now` rather than assuming yesterday: on the
    Tuesday after a long weekend the answer is the previous Friday, and a
    scanner that assumed "yesterday" would look for a session that never
    happened and then have to decide what to do about it.
    """
    config = config or ScannerConfig()
    limit = config.settings.catchup_window

    current = _as_date(now)
    for _ in range(limit + 1):
        if is_trading_day(current) and now >= due_at(current, config):
            return current
        current -= timedelta(days=1)
    return None


def is_due(scan_date: date, now: datetime, config: ScannerConfig | None = None) -> bool:
    """Whether `scan_date` is a trading day whose scan time has arrived."""
    return is_trading_day(scan_date) and now >= due_at(scan_date, config)


def within_readiness_window(
    scan_date: date, now: datetime, config: ScannerConfig | None = None
) -> bool:
    """Whether missing data for this date is still 'late' rather than 'wrong'.

    The distinction `readiness.py` draws needs a boundary, and this is it.
    Inside the window, no data means wait. Outside it, no data means the
    day is not going to be scanned and somebody should know.
    """
    return now <= window_for(scan_date, config).expires_at


def next_scan_time(
    scan_date: date, now: datetime, config: ScannerConfig | None = None
) -> datetime | None:
    """When to ask about `scan_date` again, or None if it has expired.

    Returns the due time when that is still ahead, so a caller asking too
    early is told to wait rather than told to retry immediately.
    """
    config = config or ScannerConfig()
    window = window_for(scan_date, config)
    if now < window.due_at:
        return window.due_at
    candidate = now + config.settings.retry_interval
    return candidate if candidate <= window.expires_at else None


def pending_trading_days(
    now: datetime, *, completed: set[date], config: ScannerConfig | None = None
) -> list[date]:
    """Trading days due by `now` that have not been completed, oldest first.

    Bounded by `max_catchup_days`. That bound is the operational half of
    "incremental, never re-scans full history": the architecture makes any
    date scannable, and this makes sure a scheduling mistake cannot walk
    the scanner back through fifteen years of them. Oldest first, because
    a setup's lifecycle is a sequence — scanning Thursday before Wednesday
    would advance state machines out of order.
    """
    config = config or ScannerConfig()
    limit = config.settings.catchup_window

    pending: list[date] = []
    current = _as_date(now)
    for _ in range(limit):
        if is_trading_day(current) and now >= due_at(current, config) and current not in completed:
            pending.append(current)
        current -= timedelta(days=1)
    return sorted(pending)


def _as_date(moment: datetime) -> date:
    return moment.astimezone(UTC).date() if moment.tzinfo else moment.date()
