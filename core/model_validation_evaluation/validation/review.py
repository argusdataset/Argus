"""The review gate: PENDING_REVIEW -> APPROVED / REJECTED, and what Module 20 may see.

## "Latest row" needs a sequence, not a timestamp

Migration 0008 added `sequence_number`, monotonic per run. `assigned_at`
defaults to `now()`, which is PostgreSQL's *transaction start* time, so
two assignments in one transaction share it to the microsecond — and the
`id` tiebreak is a random UUID. Ordering on those made "the current
status" a coin flip in exactly the case where it matters most: a reviewer
approving and then immediately correcting themselves. The sequence is the
same mechanism `setup_events` uses, for the same reason.

## Why this is a table and not a column

`historical_scan_status` is append-only, one row per status assignment,
current status being the latest row for a run. Module 03 chose that shape
and the reason is accountability: the decision to publish a model's numbers
as a track record should carry a permanent record of who made it and when,
and the next reviewer should not be able to overwrite the last one's
judgement. A `status` column would make the history of a contested approval
unrecoverable.

## What "structurally enforceable" means here, concretely

Module 20 is not built and this module does not build it. What this module
owes Module 20 is that "only approved scans" cannot be got wrong by
forgetting a `WHERE` clause. So the *only* function here that returns
run IDs is `approved_runs`, and there is no parameter that relaxes it.
A caller wanting the full picture calls `current_status` per run and has
to look at the answer.

The gate opens exactly one way. `open_for_review` is called by the replay
engine when a run completes, and it writes PENDING_REVIEW with no
`assigned_by_user_id` — that column is nullable precisely because this row
is the system's, not a person's. Every subsequent row requires a
reviewer: `approve` and `reject` both demand a user ID, and refuse
without one. An approval nobody's name is on is not a human review, and
this module will not record one as if it were.

## Backend only

There is no UI here and none is coming from this module — the brief is
explicit that review UI belongs to a later module. What is here is the
API-shaped surface that UI would sit on: five functions, all taking a
connection and a run ID, all returning plain data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.engine import Connection

from infra.db.enums import HistoricalScanStatus, ValidationRunStatus
from infra.db.schema.validation import historical_scan_status, model_validation_runs


class ReviewRefused(ValueError):
    """A review action that would not have been a review."""


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """One entry in a run's review history."""

    run_id: UUID
    sequence_number: int
    status: HistoricalScanStatus
    assigned_by_user_id: UUID | None
    assigned_at: datetime
    note: str | None

    @property
    def by_system(self) -> bool:
        return self.assigned_by_user_id is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "sequence_number": self.sequence_number,
            "status": self.status.value,
            "assigned_by_user_id": (
                str(self.assigned_by_user_id) if self.assigned_by_user_id else None
            ),
            "assigned_at": self.assigned_at.isoformat(),
            "note": self.note,
        }


def open_for_review(
    connection: Connection, run_id: UUID, *, note: str | None = None
) -> ReviewRecord:
    """Put a completed run in the queue, as the system rather than a person.

    Refuses a run that is not COMPLETED. A RUNNING run's numbers are
    partial and a FAILED run's are wrong; queueing either for approval
    would invite a reviewer to approve results the system already knows
    are not finished.

    Idempotent: a run already at PENDING_REVIEW is returned unchanged
    rather than accumulating duplicate rows, because a re-run of the
    completion step is not a second review event.
    """
    status = connection.execute(
        select(model_validation_runs.c.status).where(model_validation_runs.c.id == run_id)
    ).scalar_one_or_none()
    if status is None:
        raise ReviewRefused(f"No validation run {run_id}.")
    if ValidationRunStatus(status) is not ValidationRunStatus.COMPLETED:
        raise ReviewRefused(
            f"Run {run_id} is {status}, not COMPLETED. A partial or failed run's "
            "numbers must not be offered for approval as if they were results."
        )

    existing = current_status(connection, run_id)
    if existing is not None and existing.status is HistoricalScanStatus.PENDING_REVIEW:
        return existing

    return _assign(
        connection,
        run_id,
        status=HistoricalScanStatus.PENDING_REVIEW,
        user_id=None,
        note=note,
    )


def approve(
    connection: Connection, run_id: UUID, *, user_id: UUID, note: str | None = None
) -> ReviewRecord:
    """A named human approves a run's results for publication."""
    return _decide(connection, run_id, HistoricalScanStatus.APPROVED, user_id, note)


def reject(
    connection: Connection, run_id: UUID, *, user_id: UUID, note: str | None = None
) -> ReviewRecord:
    """A named human rejects a run's results.

    Rejection is not deletion and not the end: the run, its signals and
    its evaluation report all stay exactly where they are. What changes
    is that Module 20 may not serve them. A rejected run can be
    re-approved later by another reviewer, and both decisions remain in
    the history.
    """
    return _decide(connection, run_id, HistoricalScanStatus.REJECTED, user_id, note)


def current_status(connection: Connection, run_id: UUID) -> ReviewRecord | None:
    """The latest status row for a run, or None if it was never queued."""
    row = connection.execute(
        select(historical_scan_status)
        .where(historical_scan_status.c.model_validation_run_id == run_id)
        .order_by(desc(historical_scan_status.c.sequence_number))
        .limit(1)
    ).one_or_none()
    return _record(row) if row is not None else None


def review_history(connection: Connection, run_id: UUID) -> list[ReviewRecord]:
    """Every status this run has been assigned, oldest first."""
    rows = connection.execute(
        select(historical_scan_status)
        .where(historical_scan_status.c.model_validation_run_id == run_id)
        .order_by(historical_scan_status.c.sequence_number)
    ).all()
    return [_record(row) for row in rows]


def approved_runs(connection: Connection) -> list[UUID]:
    """Every run whose *current* status is APPROVED. Module 20's only door.

    "Current" is load-bearing and is why this is not a simple `WHERE
    status = 'APPROVED'`: a run approved on Monday and rejected on Tuesday
    has an APPROVED row and must not be served. The window function picks
    the latest row per run first, and only then filters.
    """
    latest = select(
        historical_scan_status.c.model_validation_run_id.label("run_id"),
        historical_scan_status.c.status.label("status"),
        func.row_number()
        .over(
            partition_by=historical_scan_status.c.model_validation_run_id,
            order_by=desc(historical_scan_status.c.sequence_number),
        )
        .label("rank"),
    ).subquery()
    return list(
        connection.execute(
            select(latest.c.run_id)
            .where(latest.c.rank == 1, latest.c.status == HistoricalScanStatus.APPROVED.value)
            .order_by(latest.c.run_id)
        ).scalars()
    )


def is_approved(connection: Connection, run_id: UUID) -> bool:
    """Whether one run may be published. Same rule as `approved_runs`."""
    record = current_status(connection, run_id)
    return record is not None and record.status is HistoricalScanStatus.APPROVED


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _decide(
    connection: Connection,
    run_id: UUID,
    status: HistoricalScanStatus,
    user_id: UUID | None,
    note: str | None,
) -> ReviewRecord:
    if user_id is None:
        raise ReviewRefused(
            f"{status.value} requires a reviewer. Only the initial PENDING_REVIEW row "
            "is the system's; a decision with nobody's name on it is not a human review."
        )
    if current_status(connection, run_id) is None:
        raise ReviewRefused(
            f"Run {run_id} has never been opened for review. Approving a run that was "
            "never queued would skip the gate rather than pass it."
        )
    return _assign(connection, run_id, status=status, user_id=user_id, note=note)


def _assign(
    connection: Connection,
    run_id: UUID,
    *,
    status: HistoricalScanStatus,
    user_id: UUID | None,
    note: str | None,
) -> ReviewRecord:
    row = connection.execute(
        historical_scan_status.insert()
        .values(
            model_validation_run_id=run_id,
            sequence_number=_next_sequence(connection, run_id),
            status=status.value,
            assigned_by_user_id=user_id,
            note=note,
        )
        .returning(
            historical_scan_status.c.model_validation_run_id,
            historical_scan_status.c.sequence_number,
            historical_scan_status.c.status,
            historical_scan_status.c.assigned_by_user_id,
            historical_scan_status.c.assigned_at,
            historical_scan_status.c.note,
        )
    ).one()
    return _record(row)


def _next_sequence(connection: Connection, run_id: UUID) -> int:
    """One past the highest sequence this run has.

    The unique constraint on `(run_id, sequence_number)` is what makes
    this safe rather than merely usual: two concurrent reviewers reading
    the same maximum both try to write the same number, and one of them
    is refused by the database instead of silently producing two rows
    that claim to be the same assignment.
    """
    highest = connection.execute(
        select(func.max(historical_scan_status.c.sequence_number)).where(
            historical_scan_status.c.model_validation_run_id == run_id
        )
    ).scalar_one()
    return 0 if highest is None else int(highest) + 1


def _record(row: Any) -> ReviewRecord:
    return ReviewRecord(
        run_id=row.model_validation_run_id,
        sequence_number=int(row.sequence_number),
        status=HistoricalScanStatus(row.status),
        assigned_by_user_id=row.assigned_by_user_id,
        assigned_at=row.assigned_at,
        note=row.note,
    )
