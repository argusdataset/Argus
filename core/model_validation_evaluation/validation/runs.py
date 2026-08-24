"""The `model_validation_runs` record: what was replayed, under what, and how it went.

A run row exists **before** the replay does any work, in `RUNNING`. That
ordering is deliberate. A run that dies halfway leaves a `RUNNING` row
naming exactly which period and which versions were in flight, which is
what makes the partial results identifiable afterwards. Creating the row
on success instead would make a crashed run indistinguishable from one
that never started, and its half-written signals indistinguishable from
anybody else's.

`model_validation_runs` is not append-only — Module 03 left it mutable so
`status` and `completed_at` can be filled in on the way through. The
gate that actually matters, `historical_scan_status`, *is* append-only.
See `review.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.scoring.engine import Lineage
from infra.db.enums import ValidationRunStatus
from infra.db.schema.validation import model_validation_runs


@dataclass(frozen=True, slots=True)
class ValidationRun:
    """One recorded replay."""

    id: UUID
    lineage: Lineage
    period_start: datetime
    period_end: datetime
    status: ValidationRunStatus

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "status": self.status.value,
            **self.lineage.as_dict(),
        }


def start_run(
    connection: Connection,
    *,
    lineage: Lineage,
    period_start: datetime,
    period_end: datetime,
    notes: str | None = None,
) -> ValidationRun:
    """Open a RUNNING row for a replay that is about to begin."""
    run_id = connection.execute(
        model_validation_runs.insert()
        .values(
            target_model_version_id=lineage.target_model_version_id,
            feature_schema_version_id=lineage.feature_schema_version_id,
            scoring_configuration_id=lineage.scoring_configuration_id,
            detection_configuration_id=lineage.detection_configuration_id,
            universe_version_id=lineage.universe_version_id,
            data_snapshot_id=lineage.data_snapshot_id,
            status=ValidationRunStatus.RUNNING.value,
            period_start=period_start,
            period_end=period_end,
            notes=notes,
        )
        .returning(model_validation_runs.c.id)
    ).scalar_one()

    return ValidationRun(
        id=run_id,
        lineage=lineage,
        period_start=period_start,
        period_end=period_end,
        status=ValidationRunStatus.RUNNING,
    )


def finish_run(
    connection: Connection,
    run_id: UUID,
    *,
    status: ValidationRunStatus,
    as_of: datetime | None = None,
    notes: str | None = None,
) -> None:
    """Close a run as COMPLETED or FAILED.

    `as_of` stamps `completed_at`. It is a plain argument like everywhere
    else in ARGUS, but note what it means here: this is the wall-clock
    moment the *run* finished, not a replay date. A replay of 2015 that
    finishes today completed today, and recording 2015 would be a lie
    about when the work was done.
    """
    values: dict[str, Any] = {
        "status": status.value,
        "completed_at": as_of or datetime.now(UTC),
    }
    if notes is not None:
        values["notes"] = notes
    connection.execute(
        model_validation_runs.update().where(model_validation_runs.c.id == run_id).values(**values)
    )


def load_run(connection: Connection, run_id: UUID) -> ValidationRun | None:
    """Read a run back, lineage included."""
    row = connection.execute(
        select(model_validation_runs).where(model_validation_runs.c.id == run_id)
    ).one_or_none()
    if row is None:
        return None
    return ValidationRun(
        id=row.id,
        lineage=Lineage(
            target_model_version_id=row.target_model_version_id,
            feature_schema_version_id=row.feature_schema_version_id,
            data_snapshot_id=row.data_snapshot_id,
            scoring_configuration_id=row.scoring_configuration_id,
            universe_version_id=row.universe_version_id,
            detection_configuration_id=row.detection_configuration_id,
        ),
        period_start=row.period_start,
        period_end=row.period_end,
        status=ValidationRunStatus(row.status),
    )
