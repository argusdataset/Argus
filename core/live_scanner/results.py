"""Reading a scan's output from storage — the shape a consumer queries.

Module 17's report flagged that `ReplayResult.signals` does not scale as
an in-memory object: it exists there because the evaluation reads it
immediately after a batch run, and nothing downstream of a *live* scan
does. So this module reads, and the scanner does not hold.

## What this is and is not

It is the query surface Module 21's Intelligence API would sit on: given a
date, what did ARGUS produce. Every function takes a connection and a
date and returns rows or counts.

It is not an API. There is no serialization contract here, no pagination,
no auth — those belong to Module 21 and building them now would fix a
shape before the consumer that has to live with it exists.

## The one thing worth getting right now

A scan's output is identified by `scan_date`, but the rows are stamped
with `as_of` — the session close plus an offset — and those are different
instants. A consumer asking "what did the 2026-03-03 scan produce" must
not be made to know about the offset. So the lookup goes through
`live_scan_runs`, which records both, rather than asking the caller to
reconstruct the cutoff. That indirection is the entire reason a run record
exists rather than just a log line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from core.live_scanner.runs import ScanRun, latest_run
from infra.db.enums import EvidenceStatus
from infra.db.schema.intelligence import signals
from infra.db.schema.setups import setups

__all__ = ["ScanResults", "scan_results", "signals_for_scan", "setups_opened_by_scan"]


@dataclass(frozen=True, slots=True)
class ScanResults:
    """What one scan date durably produced, by the numbers."""

    scan_date: date
    run: ScanRun | None
    signals_written: int = 0
    scored_signals: int = 0
    setups_opened: int = 0
    setups_qualified: int = 0
    decisions: dict[str, int] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        """Whether a completed scan exists for this date at all.

        False and "a scan that produced nothing" are different answers and
        a consumer must be able to tell them apart — the second is a
        normal quiet day, the first means nobody has looked.
        """
        return self.run is not None and self.run.succeeded

    def as_dict(self) -> dict[str, Any]:
        return {
            "scan_date": self.scan_date.isoformat(),
            "available": self.available,
            "status": self.run.status.value if self.run else None,
            "signals_written": self.signals_written,
            "scored_signals": self.scored_signals,
            "setups_opened": self.setups_opened,
            "setups_qualified": self.setups_qualified,
            "decisions": self.decisions,
            "excluded_count": self.run.excluded_count if self.run else 0,
        }


def scan_results(connection: Connection, scan_date: date) -> ScanResults:
    """Counts for one scan date, read from the stored rows.

    Counts rather than rows, deliberately. A consumer that wants the
    signals themselves calls `signals_for_scan` and gets an explicit list;
    a consumer that wants to know whether anything happened should not
    have to load a universe to find out.
    """
    run = latest_run(connection, scan_date)
    if run is None or not run.succeeded:
        return ScanResults(scan_date=scan_date, run=run)

    # Grouped on the real `evidence_status` column rather than on the
    # `decision` inside the JSONB detail. The column is indexed, is
    # CHECK-tied to whether the five numbers are present, and — the
    # deciding reason — is a column: grouping on a JSONB path expression
    # made PostgreSQL reject the query, because SQLAlchemy binds the path
    # separately in the projection and the GROUP BY.
    decisions = {
        str(row.evidence_status): int(row.count)
        for row in connection.execute(
            select(signals.c.evidence_status, func.count().label("count"))
            .where(signals.c.event_time == run.as_of)
            .group_by(signals.c.evidence_status)
        )
    }

    scored = int(
        connection.execute(
            select(func.count())
            .select_from(signals)
            .where(signals.c.event_time == run.as_of, signals.c.argus_score.is_not(None))
        ).scalar_one()
    )
    written = sum(decisions.values())

    opened = int(
        connection.execute(
            select(func.count()).select_from(setups).where(setups.c.detected_at == run.as_of)
        ).scalar_one()
    )
    qualified = int(
        connection.execute(
            select(func.count())
            .select_from(setups)
            .join(signals, signals.c.id == setups.c.qualifying_signal_id)
            .where(signals.c.event_time == run.as_of)
        ).scalar_one()
    )

    return ScanResults(
        scan_date=scan_date,
        run=run,
        signals_written=written,
        scored_signals=scored,
        setups_opened=opened,
        setups_qualified=qualified,
        decisions=decisions,
    )


def signals_for_scan(
    connection: Connection,
    scan_date: date,
    *,
    scored_only: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """The signal rows one scan wrote, newest-scoring first.

    `scored_only` defaults to True because a refusal is not a signal a
    consumer would show anyone — Module 13 writes `INSUFFICIENT_EVIDENCE`
    rows so the refusal is on the record, not so it can be displayed as a
    result. Passing False returns them, which is what an investigation
    wants.
    """
    run = latest_run(connection, scan_date)
    if run is None or not run.succeeded:
        return []

    query = select(
        signals.c.id,
        signals.c.security_id,
        signals.c.event_time,
        signals.c.argus_score,
        signals.c.confidence,
        signals.c.opportunity_score,
        signals.c.risk_score,
        signals.c.evidence_status,
        signals.c.target_model_version_id,
        signals.c.scoring_configuration_id,
    ).where(signals.c.event_time == run.as_of)

    if scored_only:
        query = query.where(signals.c.argus_score.is_not(None))
    query = query.order_by(signals.c.argus_score.desc().nullslast(), signals.c.security_id)
    if limit is not None:
        query = query.limit(limit)

    return [
        {
            "signal_id": str(row.id),
            "security_id": str(row.security_id),
            "event_time": row.event_time.isoformat(),
            "argus_score": float(row.argus_score) if row.argus_score is not None else None,
            "confidence": float(row.confidence) if row.confidence is not None else None,
            "opportunity_score": (
                float(row.opportunity_score) if row.opportunity_score is not None else None
            ),
            "risk_score": float(row.risk_score) if row.risk_score is not None else None,
            "evidence_status": row.evidence_status,
            "target_model_version_id": str(row.target_model_version_id),
            "scoring_configuration_id": str(row.scoring_configuration_id),
        }
        for row in connection.execute(query)
    ]


def setups_opened_by_scan(connection: Connection, scan_date: date) -> list[UUID]:
    """Setup IDs this scan opened. Empty for a date never scanned."""
    run = latest_run(connection, scan_date)
    if run is None or not run.succeeded:
        return []
    return list(
        connection.execute(
            select(setups.c.id).where(setups.c.detected_at == run.as_of).order_by(setups.c.id)
        ).scalars()
    )


#: The keys `ScanResults.decisions` can carry. Module 13's finer-grained
#: `ScoringDecision` (which distinguishes the two gated reasons) lives in
#: each signal's `detail`; what is stored as a column, and therefore what
#: is groupable at scale, is this.
DECISIONS: tuple[str, ...] = tuple(status.value for status in EvidenceStatus)
