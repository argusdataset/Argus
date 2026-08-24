"""Is this date's data actually here yet? — the check that keeps the scanner honest.

## Why this is a separate concept from failure

"Today's data isn't ready" and "today's scan broke" want opposite
responses. The first wants patience; the second wants a person. A scanner
that reported both as FAILED would train whoever reads its records to
ignore FAILED, which is the same as having no failure reporting at all.

So readiness is asked *before* the scan runs, answered from stored data,
and recorded as its own state.

## What "ready" means, concretely

A bar for the scan date, knowable at the scan's `as_of`, for at least
`min_universe_coverage` of the universe version's members.

Two details in that sentence are load-bearing:

**Knowable at `as_of`, not merely present.** The check filters on
`availability_time <= as_of`, the same predicate Module 07 applies
everywhere. Otherwise the scanner could see data the scan itself would
then refuse to look at, conclude it was ready, and scan an empty day.

**A fraction of the universe, not all of it.** On any real day some names
are halted, some listed this morning, and some the provider does not
deliver. Demanding everything means never scanning. But the threshold
cannot be low either, and the reason is specific rather than
aesthetic: Module 09's ranking is **cross-sectional**. A candidate pool
selected from half the universe is a *different* pool, not a smaller one —
the top 5% of half the names is not the top 5% of the names. Scanning a
half-delivered day would therefore produce signals that look normal and
are not comparable to any other day's.

## One query

Coverage is a single aggregate over the membership join, not a per-security
loop. At ten thousand names asked once a day this is the difference
between one round trip and ten thousand — and the readiness check runs
*more* often than the scan does, since a not-ready day is asked about
repeatedly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from core.live_scanner.config import ScannerConfig
from data.canonical_model.records import CanonicalTimeframe

__all__ = ["ReadinessReport", "check_readiness"]

_COVERAGE_QUERY = """
    WITH members AS (
        SELECT security_id
        FROM universe_membership
        WHERE universe_version_id = :universe_version_id
          AND listed_from <= :as_of
          AND (listed_to IS NULL OR listed_to > :as_of)
    ),
    delivered AS (
        SELECT DISTINCT o.security_id
        FROM canonical_ohlcv o
        JOIN members m ON m.security_id = o.security_id
        WHERE o.timeframe = :timeframe
          AND o.event_time >= :day_start
          AND o.event_time < :day_end
          AND o.availability_time <= :as_of
    )
    SELECT
        (SELECT count(*) FROM members)   AS universe_size,
        (SELECT count(*) FROM delivered) AS delivered_count
"""


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Whether this date can be scanned, and the numbers behind the answer."""

    scan_date: date
    as_of: datetime
    universe_size: int
    delivered: int
    required_fraction: float
    ready: bool
    reason: str

    @property
    def coverage(self) -> float | None:
        """Fraction of the universe with a bar. None for an empty universe.

        None rather than 0.0, per the rule Module 08 established: a
        universe with no members has no coverage to report, which is a
        different fact from a universe nobody delivered data for.
        """
        if self.universe_size == 0:
            return None
        return self.delivered / self.universe_size

    def as_dict(self) -> dict[str, Any]:
        return {
            "scan_date": self.scan_date.isoformat(),
            "as_of": self.as_of.isoformat(),
            "universe_size": self.universe_size,
            "delivered": self.delivered,
            "coverage": self.coverage,
            "required_fraction": self.required_fraction,
            "ready": self.ready,
            "reason": self.reason,
        }


def check_readiness(
    connection: Connection,
    *,
    scan_date: date,
    as_of: datetime,
    universe_version_id: UUID,
    config: ScannerConfig | None = None,
    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY,
) -> ReadinessReport:
    """Whether `scan_date` has enough delivered data to be worth scanning."""
    config = config or ScannerConfig()
    required = config.settings.min_universe_coverage.value

    # A bar's `event_time` is its session close, so the whole calendar day
    # in the cutoff's own zone brackets exactly one session.
    day_start = datetime.combine(scan_date, time.min, tzinfo=as_of.tzinfo)
    day_end = day_start + timedelta(days=1)

    row = connection.execute(
        text(_COVERAGE_QUERY),
        {
            "universe_version_id": universe_version_id,
            "as_of": as_of,
            "timeframe": timeframe.value,
            "day_start": day_start,
            "day_end": day_end,
        },
    ).one()

    universe_size = int(row.universe_size)
    delivered = int(row.delivered_count)

    if universe_size == 0:
        # Not "ready with 100% coverage". An empty universe means the
        # version is wrong or was never built, and scanning it would
        # produce a clean, meaningless, successful-looking run.
        return ReadinessReport(
            scan_date=scan_date,
            as_of=as_of,
            universe_size=0,
            delivered=0,
            required_fraction=required,
            ready=False,
            reason=(
                "The universe version has no members listed on this date. This is not "
                "a delivery delay — there is nothing to scan, and a successful scan of "
                "nothing would be the most misleading result this module could record."
            ),
        )

    coverage = delivered / universe_size
    if coverage < required:
        return ReadinessReport(
            scan_date=scan_date,
            as_of=as_of,
            universe_size=universe_size,
            delivered=delivered,
            required_fraction=required,
            ready=False,
            reason=(
                f"{delivered} of {universe_size} universe members have a bar knowable "
                f"at {as_of.isoformat()} ({coverage:.1%}); the bar is {required:.0%}. "
                "Module 09's ranking is cross-sectional, so a partly-delivered day "
                "produces a different candidate pool rather than a smaller one."
            ),
        )

    return ReadinessReport(
        scan_date=scan_date,
        as_of=as_of,
        universe_size=universe_size,
        delivered=delivered,
        required_fraction=required,
        ready=True,
        reason=f"{delivered} of {universe_size} members delivered ({coverage:.1%}).",
    )
