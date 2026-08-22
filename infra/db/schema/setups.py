"""Setup lifecycle and outcomes (Modules 14-15).

The lifecycle is event-sourced on purpose. `setups` has **no status
column**: a setup's current status is derived from its `setup_events`
history, which is append-only. That is what makes "quietly rewrite a
setup's history so a failure looks like something else" impossible rather
than merely discouraged.

`setups` and `setup_outcomes` are protected against DELETE and TRUNCATE
but not against UPDATE. The asymmetry is deliberate: a failed setup must
be impossible to erase, but the human review classification
(`review_confidence`, `false_positive_type`) is genuinely assigned after
the outcome is computed, and sometimes revised on re-review. Blocking
UPDATE outright would force that workflow into a supersession chain on a
table that is otherwise one row per setup. See
`infra.db.append_only` for the two guard strengths.

A failed case carries identical data richness to a successful one — same
columns, same completeness expectations. Nothing about the schema treats
failures as second-class.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Interval,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from infra.db.enums import (
    FalsePositiveType,
    MarketState,
    OutcomeStatus,
    ReviewConfidence,
    SetupLifecycleStatus,
)
from infra.db.metadata import metadata, pg_enum

setups = Table(
    "setups",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # A setup is created when a candidate reaches CONSOLIDATION. The
    # lifecycle tracks alongside market_state, not instead of it.
    Column("detected_at", DateTime(timezone=True), nullable=False),
    # NOTE: there is deliberately no `status` column. Current status is
    # derived from setup_events. Adding one here would reintroduce the
    # mutable field the event sourcing exists to avoid.
    Column(
        "target_model_version_id",
        UUID(as_uuid=True),
        ForeignKey("target_model_version.id", ondelete="RESTRICT"),
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
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_setups_security_time", "security_id", "detected_at"),
    comment="A detected setup. Current lifecycle status is derived from setup_events.",
)

setup_events = Table(
    "setup_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "setup_id",
        UUID(as_uuid=True),
        ForeignKey("setups.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # Monotonic per setup, so the event order is unambiguous even when
    # two events share a timestamp.
    Column("sequence_number", Integer, nullable=False),
    # The lifecycle stage this event moves the setup into:
    # DETECTION -> QUALIFICATION -> ACTIVE -> OUTCOME.
    Column(
        "lifecycle_status",
        pg_enum(SetupLifecycleStatus, "setup_lifecycle_status"),
        nullable=False,
    ),
    Column("event_type", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("setup_id", "sequence_number", name="uq_setup_event_sequence"),
    CheckConstraint("sequence_number >= 0", name="sequence_non_negative"),
    Index("ix_setup_events_setup", "setup_id", "sequence_number"),
    comment="Append-only setup lifecycle events. The source of truth for setup status.",
)

setup_outcomes = Table(
    "setup_outcomes",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "setup_id",
        UUID(as_uuid=True),
        ForeignKey("setups.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    # Never forced into a binary win/loss — EXPIRED, INVALIDATED and
    # NO_VALID_OUTCOME are first-class results, not failures in disguise.
    Column(
        "outcome_status",
        pg_enum(OutcomeStatus, "outcome_status"),
        nullable=False,
    ),
    # Maximum favourable / adverse excursion, and how long each took.
    Column("mfe", Numeric(12, 6), nullable=True),
    Column("mae", Numeric(12, 6), nullable=True),
    Column("time_to_mfe", Interval, nullable=True),
    Column("time_to_mae", Interval, nullable=True),
    Column("outcome_window", Interval, nullable=True),
    Column("realized_return", Numeric(12, 6), nullable=True),
    Column("benchmark_relative_return", Numeric(12, 6), nullable=True),
    Column("volatility_adjusted_outcome", Numeric(12, 6), nullable=True),
    Column(
        "market_regime_at_outcome",
        pg_enum(MarketState, "market_state_enum"),
        nullable=True,
    ),
    # --- human review classification, assigned at or after outcome ---
    Column(
        "review_confidence",
        pg_enum(ReviewConfidence, "review_confidence"),
        nullable=True,
    ),
    # NULL for a setup that was not a false positive.
    Column(
        "false_positive_type",
        pg_enum(FalsePositiveType, "false_positive_type"),
        nullable=True,
    ),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_setup_outcomes_status", "outcome_status"),
    Index("ix_setup_outcomes_false_positive", "false_positive_type"),
    comment="Outcome and case record for a setup. Failures carry the same detail as successes.",
)
