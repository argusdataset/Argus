"""record every attempt at a daily scan, and why each one ended as it did

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-24

Module 18 needs its own run record. `model_validation_runs` is the wrong
home for it and the reason is not tidiness: every row there is a candidate
for Module 17's review gate, so 250 daily scans a year would bury the runs
that genuinely need a human decision.

The status enum carries more states than `ValidationRunStatus`, and the
extra ones are the whole difference between a batch job and an unattended
process — see `infra/db/enums.LiveScanStatus`. The short version:
`DATA_NOT_READY` is not a failure and must not be recorded as one, or
whoever reads these rows learns to ignore FAILED.

One row per `(scan_date, attempt)`. A retry is a new row rather than an
overwrite, so a date that took four tries can still be investigated.

Not guarded as append-only. Module 03's guards exist for rows whose
editing would rewrite what ARGUS believed at a point in time; a scan run
is an operational record, nothing downstream computes a statistic from
it, and its `status` legitimately moves RUNNING -> terminal once. Same
reasoning Module 03 applied to `model_validation_runs`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS_ENUM = "live_scan_status"
STATUS_VALUES = (
    "RUNNING",
    "COMPLETED",
    "COMPLETED_WITH_EXCLUSIONS",
    "DATA_NOT_READY",
    "FAILED",
)


def upgrade() -> None:
    status = postgresql.ENUM(*STATUS_VALUES, name=STATUS_ENUM, create_type=False)
    status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "live_scan_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("scan_date", sa.Date(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("target_model_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("feature_schema_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scoring_configuration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("detection_configuration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("universe_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("data_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "excluded_securities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["target_model_version_id"],
            ["target_model_version.id"],
            name=op.f("fk_live_scan_runs_target_model_version_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["feature_schema_version_id"],
            ["feature_schema_version.id"],
            name=op.f("fk_live_scan_runs_feature_schema_version_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_configuration_id"],
            ["scoring_configuration.id"],
            name=op.f("fk_live_scan_runs_scoring_configuration_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["detection_configuration_id"],
            ["detection_configuration.id"],
            name=op.f("fk_live_scan_runs_detection_configuration_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["universe_version_id"],
            ["universe_version.id"],
            name=op.f("fk_live_scan_runs_universe_version_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["data_snapshot_id"],
            ["data_snapshot.id"],
            name=op.f("fk_live_scan_runs_data_snapshot_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("scan_date", "attempt", name="uq_live_scan_date_attempt"),
        sa.CheckConstraint("attempt >= 0", name=op.f("ck_live_scan_runs_attempt_non_negative")),
        comment="One attempt at one trading day's live scan (Module 18).",
    )
    op.create_index("ix_live_scan_date", "live_scan_runs", ["scan_date", "attempt"])
    op.create_index("ix_live_scan_status", "live_scan_runs", ["status", "scan_date"])


def downgrade() -> None:
    op.drop_index("ix_live_scan_status", table_name="live_scan_runs")
    op.drop_index("ix_live_scan_date", table_name="live_scan_runs")
    op.drop_table("live_scan_runs")
    postgresql.ENUM(name=STATUS_ENUM).drop(op.get_bind(), checkfirst=True)
