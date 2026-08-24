"""The scan-run record: what was attempted, how it ended, and what was excluded.

Every function here is a small wrapper over `live_scan_runs`, and the
interesting decisions are all about *when a row exists* rather than what
is in it.

## A row is opened before the work, not after

Same reasoning as Module 17's `start_run`, with more force: a scan that
dies mid-way must leave something behind saying which date was in flight.
The alternative is that a crash and a scan that never started look
identical, which for an unattended process is the difference between
noticing and not.

## A retry is a new row

`(scan_date, attempt)` is unique and `attempt` is monotonic per date. A
date that took four tries leaves four rows, which is what anyone
investigating a flaky scanner actually wants to see. Module 17 learned
this the expensive way: `historical_scan_status` originally ordered by a
timestamp that turned out to be transaction-start time, so several
assignments in one transaction ordered arbitrarily. An explicit monotonic
counter, with the database enforcing uniqueness on it, is the mechanism
that does not have that failure mode.

## `completed_dates` is deliberately narrow

Only COMPLETED and COMPLETED_WITH_EXCLUSIONS count as done. A date whose
last attempt was DATA_NOT_READY or FAILED is *not* done and must come back
around in catch-up — treating "we tried" as "we finished" is exactly how a
scanner skips a day and never notices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.engine import Connection

from core.scoring.engine import Lineage
from infra.db.enums import LiveScanStatus
from infra.db.schema.live_scanner import live_scan_runs

__all__ = [
    "TERMINAL_SUCCESS",
    "ScanRun",
    "completed_dates",
    "finish_run",
    "latest_run",
    "next_attempt",
    "record_not_ready",
    "run_history",
    "runs_by_status",
    "start_run",
]

#: The two states that mean a date does not need scanning again.
TERMINAL_SUCCESS: frozenset[LiveScanStatus] = frozenset(
    {LiveScanStatus.COMPLETED, LiveScanStatus.COMPLETED_WITH_EXCLUSIONS}
)


@dataclass(frozen=True, slots=True)
class ScanRun:
    """One attempt at one date."""

    id: UUID
    scan_date: date
    as_of: datetime
    attempt: int
    status: LiveScanStatus
    excluded_securities: list[dict[str, Any]] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    note: str | None = None
    finished_at: datetime | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in TERMINAL_SUCCESS

    @property
    def excluded_count(self) -> int:
        return len(self.excluded_securities)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "scan_date": self.scan_date.isoformat(),
            "as_of": self.as_of.isoformat(),
            "attempt": self.attempt,
            "status": self.status.value,
            "excluded_securities": self.excluded_securities,
            "excluded_count": self.excluded_count,
            "detail": self.detail,
            "note": self.note,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


def next_attempt(connection: Connection, scan_date: date) -> int:
    """One past the highest attempt recorded for this date.

    The unique constraint on `(scan_date, attempt)` is what makes this
    read-then-write safe rather than merely usual: two schedulers firing
    at once both read the same maximum, and one of them is refused by the
    database instead of silently writing a second row claiming to be the
    same attempt.
    """
    highest = connection.execute(
        select(func.max(live_scan_runs.c.attempt)).where(live_scan_runs.c.scan_date == scan_date)
    ).scalar_one()
    return 0 if highest is None else int(highest) + 1


def start_run(
    connection: Connection,
    *,
    scan_date: date,
    as_of: datetime,
    lineage: Lineage,
    attempt: int | None = None,
    note: str | None = None,
) -> ScanRun:
    """Open a RUNNING row for a scan about to begin."""
    number = next_attempt(connection, scan_date) if attempt is None else attempt
    row = connection.execute(
        live_scan_runs.insert()
        .values(
            scan_date=scan_date,
            as_of=as_of,
            attempt=number,
            status=LiveScanStatus.RUNNING.value,
            target_model_version_id=lineage.target_model_version_id,
            feature_schema_version_id=lineage.feature_schema_version_id,
            scoring_configuration_id=lineage.scoring_configuration_id,
            detection_configuration_id=lineage.detection_configuration_id,
            universe_version_id=lineage.universe_version_id,
            data_snapshot_id=lineage.data_snapshot_id,
            note=note,
        )
        .returning(*_COLUMNS)
    ).one()
    return _record(row)


def finish_run(
    connection: Connection,
    run_id: UUID,
    *,
    status: LiveScanStatus,
    detail: dict[str, Any] | None = None,
    excluded: list[dict[str, Any]] | None = None,
    note: str | None = None,
    finished_at: datetime | None = None,
) -> ScanRun:
    """Close a run in a terminal state.

    `finished_at` is a plain argument like every other timestamp in ARGUS,
    but note what it means here: the wall-clock instant the *process*
    stopped, not a replay date. A catch-up scan of last Thursday finished
    today, and recording Thursday would be a lie about when the work
    happened while the scan's own `as_of` correctly says Thursday.
    """
    row = connection.execute(
        live_scan_runs.update()
        .where(live_scan_runs.c.id == run_id)
        .values(
            status=status.value,
            detail=detail if detail is not None else {},
            excluded_securities=excluded if excluded is not None else [],
            note=note,
            finished_at=finished_at or datetime.now(UTC),
        )
        .returning(*_COLUMNS)
    ).one()
    return _record(row)


def record_not_ready(
    connection: Connection,
    *,
    scan_date: date,
    as_of: datetime,
    lineage: Lineage,
    detail: dict[str, Any],
    note: str,
) -> ScanRun:
    """Record that a date could not be scanned because its data is absent.

    A row rather than silence, and a distinct status rather than FAILED.
    Silence would make "we are waiting" indistinguishable from "nobody
    ran"; FAILED would put a routine, expected, self-resolving condition
    in the same bucket as a broken scanner, and whoever reads these rows
    would learn to skim past both.
    """
    run = start_run(connection, scan_date=scan_date, as_of=as_of, lineage=lineage)
    return finish_run(
        connection,
        run.id,
        status=LiveScanStatus.DATA_NOT_READY,
        detail=detail,
        note=note,
    )


def latest_run(connection: Connection, scan_date: date) -> ScanRun | None:
    """The most recent attempt at this date, or None if never attempted."""
    row = connection.execute(
        select(*_COLUMNS)
        .where(live_scan_runs.c.scan_date == scan_date)
        .order_by(desc(live_scan_runs.c.attempt))
        .limit(1)
    ).one_or_none()
    return _record(row) if row is not None else None


def run_history(connection: Connection, scan_date: date) -> list[ScanRun]:
    """Every attempt at this date, in order."""
    rows = connection.execute(
        select(*_COLUMNS)
        .where(live_scan_runs.c.scan_date == scan_date)
        .order_by(live_scan_runs.c.attempt)
    ).all()
    return [_record(row) for row in rows]


def completed_dates(
    connection: Connection, *, since: date | None = None, until: date | None = None
) -> set[date]:
    """Dates whose scan finished successfully — catch-up's "already done" set.

    A date appears here if *any* attempt succeeded, which is the right
    reading: a date that failed twice and then worked is done. Only the
    two success states count; DATA_NOT_READY and FAILED leave the date
    outstanding, because treating "we tried" as "we finished" is how a
    scanner skips a day and never notices.
    """
    query = select(live_scan_runs.c.scan_date).where(
        live_scan_runs.c.status.in_([status.value for status in TERMINAL_SUCCESS])
    )
    if since is not None:
        query = query.where(live_scan_runs.c.scan_date >= since)
    if until is not None:
        query = query.where(live_scan_runs.c.scan_date <= until)
    return set(connection.execute(query.distinct()).scalars())


def runs_by_status(
    connection: Connection, status: LiveScanStatus, *, limit: int | None = None
) -> list[ScanRun]:
    """Every run in one state, newest date first.

    The read a Module 23 observability layer would make: "show me the
    FAILED ones", or "how many dates are sitting in DATA_NOT_READY".
    """
    query = (
        select(*_COLUMNS)
        .where(live_scan_runs.c.status == status.value)
        .order_by(desc(live_scan_runs.c.scan_date), desc(live_scan_runs.c.attempt))
    )
    if limit is not None:
        query = query.limit(limit)
    return [_record(row) for row in connection.execute(query)]


_COLUMNS = (
    live_scan_runs.c.id,
    live_scan_runs.c.scan_date,
    live_scan_runs.c.as_of,
    live_scan_runs.c.attempt,
    live_scan_runs.c.status,
    live_scan_runs.c.excluded_securities,
    live_scan_runs.c.detail,
    live_scan_runs.c.note,
    live_scan_runs.c.finished_at,
)


def _record(row: Any) -> ScanRun:
    return ScanRun(
        id=row.id,
        scan_date=row.scan_date,
        as_of=row.as_of,
        attempt=int(row.attempt),
        status=LiveScanStatus(row.status),
        excluded_securities=list(row.excluded_securities or []),
        detail=dict(row.detail or {}),
        note=row.note,
        finished_at=row.finished_at,
    )
