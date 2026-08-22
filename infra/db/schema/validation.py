"""Model validation, evaluation, and the public-results review gate (Module 17).

`historical_scan_status` is modelled as an append-only *table* rather than
a column on `model_validation_runs`. The reason is accountability: the
gate that decides whether a model's numbers may reach the public page
should carry a record of who moved it and when, and that record should
not be overwritable by the next reviewer. Current status is therefore the
latest row for a run, in the same derive-don't-mutate style as setup
lifecycle status.

Module 20 (Public Stats API) may only serve runs whose current status is
APPROVED. That is what stops an unvalidated model's numbers from being
published as if they were a track record.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from infra.db.enums import HistoricalScanStatus, ValidationRunStatus
from infra.db.metadata import metadata, pg_enum

model_validation_runs = Table(
    "model_validation_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    # The full configuration this run exercised. Recording every ID is
    # what makes the run repeatable and its results attributable to a
    # specific model version rather than to "whatever was deployed".
    Column(
        "target_model_version_id",
        UUID(as_uuid=True),
        ForeignKey("target_model_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "feature_schema_version_id",
        UUID(as_uuid=True),
        ForeignKey("feature_schema_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "scoring_configuration_id",
        UUID(as_uuid=True),
        ForeignKey("scoring_configuration.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "detection_configuration_id",
        UUID(as_uuid=True),
        ForeignKey("detection_configuration.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "universe_version_id",
        UUID(as_uuid=True),
        ForeignKey("universe_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "data_snapshot_id",
        UUID(as_uuid=True),
        ForeignKey("data_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "status",
        pg_enum(ValidationRunStatus, "validation_run_status"),
        nullable=False,
    ),
    # The historical window the run covered.
    Column("period_start", DateTime(timezone=True), nullable=False),
    Column("period_end", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("notes", Text, nullable=True),
    Index("ix_validation_runs_model", "target_model_version_id", "started_at"),
    comment="A model validation run over PIT-correct historical data.",
)

model_evaluation_reports = Table(
    "model_evaluation_reports",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "model_validation_run_id",
        UUID(as_uuid=True),
        ForeignKey("model_validation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # Headline metrics are columns because they are compared across runs
    # and sorted on; the long tail (calibration curves, per-regime and
    # benchmark-relative breakdowns, MFE/MAE aggregates, drawdown) lives
    # in the JSONB payload, where Module 17 owns the shape.
    Column("precision", Numeric(8, 6), nullable=True),
    Column("recall", Numeric(8, 6), nullable=True),
    Column("hit_rate", Numeric(8, 6), nullable=True),
    Column("false_positive_rate", Numeric(8, 6), nullable=True),
    Column("expectancy", Numeric(12, 6), nullable=True),
    Column("sample_size", Integer, nullable=False),
    Column("generated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("metrics", JSONB, nullable=False),
    CheckConstraint("sample_size >= 0", name="sample_size_non_negative"),
    CheckConstraint(
        "precision IS NULL OR (precision >= 0 AND precision <= 1)",
        name="precision_in_range",
    ),
    CheckConstraint("recall IS NULL OR (recall >= 0 AND recall <= 1)", name="recall_in_range"),
    CheckConstraint(
        "hit_rate IS NULL OR (hit_rate >= 0 AND hit_rate <= 1)",
        name="hit_rate_in_range",
    ),
    Index("ix_evaluation_reports_run", "model_validation_run_id"),
    comment="Metrics derived from a validation run's predictions and actual outcomes.",
)

historical_scan_status = Table(
    "historical_scan_status",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "model_validation_run_id",
        UUID(as_uuid=True),
        ForeignKey("model_validation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "status",
        pg_enum(HistoricalScanStatus, "historical_scan_status_enum"),
        nullable=False,
    ),
    # Nullable: the initial PENDING_REVIEW row is set by the system, not
    # by a person.
    Column("assigned_by_user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=True),
    Column("assigned_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("note", Text, nullable=True),
    Index("ix_scan_status_run", "model_validation_run_id", "assigned_at"),
    comment="Append-only review-gate history. Current status is the latest row per run.",
)
