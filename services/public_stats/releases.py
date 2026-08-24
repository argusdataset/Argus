"""Whether a live-tracked outcome may be published — the decision, and the mechanism.

## The question this module had to answer

Module 17 gates historical results: a validation run's numbers reach the
public page only after a named human moves it to APPROVED. Module 18
deliberately does **not** gate live scans, and its reasoning was right —
a scan succeeding is the system doing its job, not a result under review,
and putting 250 daily scans a year into the review queue would bury the
runs that need a decision.

But that reasoning is about *scans*, not about *outcomes*. When a setup
the live scanner opened in March reaches an outcome in June, Module 15
records it and nothing has ever said whether the public page may count
it. No prior module answered this, and both answers are defensible.

## The decision: yes, live outcomes need approval, gated by window

**The case for not gating them** is real and worth stating properly,
because dismissing it would be dishonest. A live outcome is not a claim
somebody made; it is a measurement a deterministic pipeline produced,
under a model version that was itself reviewed and approved. On that
reading, what got approved was *the model*, and the outcomes it goes on
to produce are just that model running. Gating each one amounts to
re-approving the model every day, which is ceremony rather than review.

**It loses to two specific facts about this system.**

First, an outcome is not purely mechanical. Module 15's success criterion
— +10% / -5% / 60 days — is an admitted, invented placeholder, and so are
`review_confidence`'s automated classification policy and
`false_positive_type`'s A–D boundaries. Module 15 said explicitly that
these need measuring against actual human review agreement. An outcome
therefore carries judgements nobody has validated, and publishing it
unreviewed publishes those judgements as though they were settled.

Second, and decisively: without a gate, **nobody ever decides to publish
any particular number.** The public statistic simply moves as outcomes
land. There is no moment at which a person looked at what the page was
about to claim and said yes. For a system whose stated purpose is to be
checkable by strangers — *"don't manufacture demand, create undeniable
value"* — that is the wrong default, and it is wrong in the direction
that flatters ARGUS, because the failure mode of an ungated pipeline is
publishing an inflated number faster than anyone notices.

## Why a window rather than an outcome

Gating each outcome individually would be precise and unusable: outcomes
arrive continuously, one per concluded setup, and a queue of thousands of
one-line approvals is a queue nobody reads carefully. It would also stall
the page behind whatever the review backlog happened to be.

So the reviewable unit is a **window**: a date range plus the
`data_snapshot_id` that labelled its outcomes. A reviewer approves
"outcomes that concluded in March, under this criterion", which is a
thing a person can actually examine. Both halves are pinned because both
matter — "which setups" and "under what definition of success" are
different questions, and a published number needs both to be
reproducible.

The honest cost, stated rather than hidden: **public statistics do not
update continuously.** They update when a window is approved. The
project's earlier design anticipated continuous updates, and this
deliberately does not deliver that — the trade is freshness for the
guarantee that every published number was published on purpose. A
project that wanted both could approve windows on a short cadence; that
is an operational choice, not a change to this mechanism.

## What is *not* gated here

Module 18's `live_scan_runs` stays ungated, exactly as Module 19 flagged.
Nothing in this module joins a successful scan to publishable results —
the gate is on outcomes, not on scans, and a scan completing grants
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.engine import Connection

from infra.db.enums import HistoricalScanStatus
from infra.db.schema.public_stats import public_release_windows
from services.public_stats.errors import (
    RELEASE_NOT_FOUND,
    REVIEW_REFUSED,
    PublicStatsError,
)

__all__ = [
    "ReleaseWindow",
    "approve_window",
    "approved_windows",
    "current_window_status",
    "open_window_for_review",
    "reject_window",
    "window_history",
]


@dataclass(frozen=True, slots=True)
class ReleaseWindow:
    """One decision about one window of live outcomes."""

    period_start: date
    period_end: date
    data_snapshot_id: UUID
    sequence_number: int
    status: HistoricalScanStatus
    assigned_by_user_id: UUID | None
    assigned_at: datetime
    note: str | None

    @property
    def key(self) -> tuple[date, date]:
        return (self.period_start, self.period_end)

    @property
    def by_system(self) -> bool:
        return self.assigned_by_user_id is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "data_snapshot_id": str(self.data_snapshot_id),
            "sequence_number": self.sequence_number,
            "status": self.status.value,
            "assigned_by_user_id": (
                str(self.assigned_by_user_id) if self.assigned_by_user_id else None
            ),
            "assigned_at": self.assigned_at.isoformat(),
            "note": self.note,
        }


def open_window_for_review(
    connection: Connection,
    *,
    period_start: date,
    period_end: date,
    data_snapshot_id: UUID,
    note: str | None = None,
) -> ReleaseWindow:
    """Queue a window of live outcomes for a human to look at.

    Written by the system, so `assigned_by_user_id` is NULL — the same
    asymmetry Module 17's gate uses, and for the same reason: the initial
    PENDING_REVIEW row is nobody's decision, it is a request for one.

    Idempotent. Re-queueing a window already pending returns it unchanged
    rather than stacking duplicate requests, because running the
    queue-for-review step twice is not two review events.
    """
    if period_end < period_start:
        raise PublicStatsError(
            REVIEW_REFUSED,
            f"A release window ends before it starts: {period_start} to {period_end}.",
            status=422,
        )

    existing = current_window_status(connection, period_start, period_end)
    if existing is not None and existing.status is HistoricalScanStatus.PENDING_REVIEW:
        return existing

    return _assign(
        connection,
        period_start=period_start,
        period_end=period_end,
        data_snapshot_id=data_snapshot_id,
        status=HistoricalScanStatus.PENDING_REVIEW,
        user_id=None,
        note=note,
    )


def approve_window(
    connection: Connection,
    *,
    period_start: date,
    period_end: date,
    user_id: UUID,
    note: str | None = None,
) -> ReleaseWindow:
    """A named human approves this window's outcomes for publication."""
    return _decide(
        connection,
        period_start=period_start,
        period_end=period_end,
        status=HistoricalScanStatus.APPROVED,
        user_id=user_id,
        note=note,
    )


def reject_window(
    connection: Connection,
    *,
    period_start: date,
    period_end: date,
    user_id: UUID,
    note: str | None = None,
) -> ReleaseWindow:
    """A named human withdraws this window from publication.

    Rejection after approval is the case that matters most: the numbers
    were on the page and now must not be. The snapshot machinery treats a
    shrinking approved set as a withdrawal and refuses to serve a stale
    payload — see `snapshots.py`.
    """
    return _decide(
        connection,
        period_start=period_start,
        period_end=period_end,
        status=HistoricalScanStatus.REJECTED,
        user_id=user_id,
        note=note,
    )


def current_window_status(
    connection: Connection, period_start: date, period_end: date
) -> ReleaseWindow | None:
    """The latest decision about this window, or None if never queued."""
    row = connection.execute(
        select(public_release_windows)
        .where(
            public_release_windows.c.period_start == period_start,
            public_release_windows.c.period_end == period_end,
        )
        .order_by(desc(public_release_windows.c.sequence_number))
        .limit(1)
    ).one_or_none()
    return _record(row) if row is not None else None


def window_history(
    connection: Connection, period_start: date, period_end: date
) -> list[ReleaseWindow]:
    """Every decision about this window, oldest first."""
    rows = connection.execute(
        select(public_release_windows)
        .where(
            public_release_windows.c.period_start == period_start,
            public_release_windows.c.period_end == period_end,
        )
        .order_by(public_release_windows.c.sequence_number)
    ).all()
    return [_record(row) for row in rows]


def approved_windows(connection: Connection) -> list[ReleaseWindow]:
    """Every window whose *current* status is APPROVED. The live half of the gate.

    "Current" is load-bearing, and the query shape is Module 17's
    `approved_runs` for the same reason: a window approved on Monday and
    rejected on Tuesday has an APPROVED row and must not be served. The
    window function picks the latest decision per window first, and only
    then filters.

    This is the **only** function in this module that returns publishable
    live windows, and it has no parameter that relaxes it.
    """
    latest = select(
        public_release_windows.c.period_start.label("period_start"),
        public_release_windows.c.period_end.label("period_end"),
        public_release_windows.c.data_snapshot_id.label("data_snapshot_id"),
        public_release_windows.c.sequence_number.label("sequence_number"),
        public_release_windows.c.status.label("status"),
        public_release_windows.c.assigned_by_user_id.label("assigned_by_user_id"),
        public_release_windows.c.assigned_at.label("assigned_at"),
        public_release_windows.c.note.label("note"),
        func.row_number()
        .over(
            partition_by=(
                public_release_windows.c.period_start,
                public_release_windows.c.period_end,
            ),
            order_by=desc(public_release_windows.c.sequence_number),
        )
        .label("rank"),
    ).subquery()

    rows = connection.execute(
        select(latest)
        .where(
            latest.c.rank == 1,
            latest.c.status == HistoricalScanStatus.APPROVED.value,
        )
        .order_by(latest.c.period_start, latest.c.period_end)
    ).all()
    return [_record(row) for row in rows]


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _decide(
    connection: Connection,
    *,
    period_start: date,
    period_end: date,
    status: HistoricalScanStatus,
    user_id: UUID | None,
    note: str | None,
) -> ReleaseWindow:
    if user_id is None:
        raise PublicStatsError(
            REVIEW_REFUSED,
            f"{status.value} requires a reviewer. Only the initial PENDING_REVIEW row is "
            "the system's; a decision with nobody's name on it is not a human review, and "
            "this gate exists precisely so that somebody decided.",
            status=422,
        )

    existing = current_window_status(connection, period_start, period_end)
    if existing is None:
        raise PublicStatsError(
            RELEASE_NOT_FOUND,
            f"No release window {period_start}..{period_end} has been opened for review. "
            "Approving a window that was never queued would skip the gate rather than "
            "pass it.",
            status=404,
        )

    return _assign(
        connection,
        period_start=period_start,
        period_end=period_end,
        # Carried from the window's own history rather than taken again:
        # a reviewer approves the window that was queued, not a
        # re-specified one.
        data_snapshot_id=existing.data_snapshot_id,
        status=status,
        user_id=user_id,
        note=note,
    )


def _assign(
    connection: Connection,
    *,
    period_start: date,
    period_end: date,
    data_snapshot_id: UUID,
    status: HistoricalScanStatus,
    user_id: UUID | None,
    note: str | None,
) -> ReleaseWindow:
    row = connection.execute(
        public_release_windows.insert()
        .values(
            period_start=period_start,
            period_end=period_end,
            data_snapshot_id=data_snapshot_id,
            sequence_number=_next_sequence(connection, period_start, period_end),
            status=status.value,
            assigned_by_user_id=user_id,
            note=note,
        )
        .returning(*_COLUMNS)
    ).one()
    return _record(row)


def _next_sequence(connection: Connection, period_start: date, period_end: date) -> int:
    """One past this window's highest sequence.

    The unique constraint on `(period_start, period_end, sequence_number)`
    is what makes the read-then-write safe rather than merely usual: two
    reviewers acting at once both read the same maximum, and one is
    refused by the database instead of silently writing a second row
    claiming to be the same decision.
    """
    highest = connection.execute(
        select(func.max(public_release_windows.c.sequence_number)).where(
            public_release_windows.c.period_start == period_start,
            public_release_windows.c.period_end == period_end,
        )
    ).scalar_one()
    return 0 if highest is None else int(highest) + 1


_COLUMNS = (
    public_release_windows.c.period_start,
    public_release_windows.c.period_end,
    public_release_windows.c.data_snapshot_id,
    public_release_windows.c.sequence_number,
    public_release_windows.c.status,
    public_release_windows.c.assigned_by_user_id,
    public_release_windows.c.assigned_at,
    public_release_windows.c.note,
)


def _record(row: Any) -> ReleaseWindow:
    return ReleaseWindow(
        period_start=row.period_start,
        period_end=row.period_end,
        data_snapshot_id=row.data_snapshot_id,
        sequence_number=int(row.sequence_number),
        status=HistoricalScanStatus(row.status),
        assigned_by_user_id=row.assigned_by_user_id,
        assigned_at=row.assigned_at,
        note=row.note,
    )
