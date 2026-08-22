"""Gap detection in canonical time-series data.

Advisory only, matching Module 05's philosophy for its own validation:
flag, never block or silently repair. ARGUS exists to find extreme,
unusual price action; a detector that "fixed" a gap by interpolating
would be inventing data, and a detector that raised would abort a batch
job over what might be a genuinely unlisted stretch (pre-IPO, a trading
halt). A `GapReport` is a fact for an operator to look at, not a decision
this module makes on its own.

This checks whether data was *ingested* for expected trading days, which
is a different question from PIT enforcement — it is not filtered by
`availability_time`, because "is there a hole in what we have" and "what
did we know on a given date" are genuinely different questions with
different callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.data_validation.calendar import expected_trading_days
from data.canonical_model.records import CanonicalTimeframe
from infra.db.schema.canonical import canonical_ohlcv


@dataclass(frozen=True, slots=True)
class GapReport:
    """Trading days with no ingested bar, for one security over one window."""

    security_id: UUID
    start: date
    end: date
    missing_dates: tuple[date, ...] = field(default_factory=tuple)
    expected_count: int = 0

    @property
    def present_count(self) -> int:
        return self.expected_count - len(self.missing_dates)

    def __bool__(self) -> bool:
        """True when the window is complete — no gap to report."""
        return len(self.missing_dates) == 0


def detect_gaps(
    connection: Connection,
    security_id: UUID,
    start: date,
    end: date,
    *,
    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY,
) -> GapReport:
    """Expected trading days in `[start, end]` with no ingested bar.

    Deliberately not PIT-filtered: this asks "is there a hole in what we
    have ingested", independent of when each bar became available. A
    caller checking today's data completeness and one replaying 2015
    would otherwise need different tools for the same question.
    """
    expected = expected_trading_days(start, end)
    if not expected:
        return GapReport(security_id=security_id, start=start, end=end)

    present_dates = {
        row.event_time.date()
        for row in connection.execute(
            select(canonical_ohlcv.c.event_time).where(
                canonical_ohlcv.c.security_id == security_id,
                canonical_ohlcv.c.timeframe == timeframe.value,
                canonical_ohlcv.c.event_time >= _start_of(start),
                canonical_ohlcv.c.event_time <= _end_of(end),
            )
        )
    }

    missing = tuple(day for day in expected if day not in present_dates)
    return GapReport(
        security_id=security_id,
        start=start,
        end=end,
        missing_dates=missing,
        expected_count=len(expected),
    )


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _end_of(day: date) -> datetime:
    return datetime.combine(day, time.max, tzinfo=UTC)
