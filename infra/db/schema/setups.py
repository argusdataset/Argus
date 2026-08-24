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
    text,
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
    # Added in migration 0007. Module 15 found this missing and had to
    # take it from the caller per-call; for a replay spanning years and
    # several schema versions, "the caller remembered correctly" is not a
    # record of anything.
    Column(
        "feature_schema_version_id",
        UUID(as_uuid=True),
        ForeignKey("feature_schema_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # The signals row that cleared the qualification bar, added in
    # migration 0007. NULL means "not qualified yet" — a fact about the
    # setup, not a missing value — and is the normal case, because
    # detection deliberately does not require a score.
    Column(
        "qualifying_signal_id",
        UUID(as_uuid=True),
        ForeignKey("signals.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    # A terminal marker, NOT a status column: NULL while the setup is
    # open, set to the terminal event's occurred_at when one is written.
    # It exists so the partial unique index below has a predicate to use —
    # openness is derived from setup_events, and an index predicate cannot
    # query another table. Added in migration 0006, which explains why
    # migration 0005's shape did not transfer unchanged.
    #
    # The projection rule from market_state applies: the log is
    # authoritative, this is rebuildable from it, and when they disagree
    # the log is right. core/lifecycle/derivation.py never reads this.
    Column("concluded_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # At most one open setup per security. Two would make "which setup
    # does this candidate belong to" ambiguous at every later scan.
    Index(
        "uq_setups_open_per_security",
        "security_id",
        unique=True,
        postgresql_where=text("concluded_at IS NULL"),
    ),
    Index("ix_setups_security_time", "security_id", "detected_at"),
    Index("ix_setups_qualifying_signal", "qualifying_signal_id"),
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
    # The PIT cutoff and outcome definition this row was computed under.
    # Every other result table in ARGUS carries one; this table went
    # without until migration 0006, which would have made the single most
    # consequential result in the system the one nobody could re-derive.
    Column(
        "data_snapshot_id",
        UUID(as_uuid=True),
        ForeignKey("data_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # One outcome per setup **per snapshot**, not per setup. Migration
    # 0007 explains the choice at length; the short version is that
    # data_snapshot_id already pins the PIT cutoff and the success
    # criterion, so re-running under a revised criterion is a different
    # legitimate answer rather than a correction of the first one.
    UniqueConstraint("setup_id", "data_snapshot_id", name="uq_setup_outcomes_setup_snapshot"),
    Index("ix_setup_outcomes_snapshot", "data_snapshot_id"),
    Index("ix_setup_outcomes_status", "outcome_status"),
    Index("ix_setup_outcomes_false_positive", "false_positive_type"),
    comment="Outcome and case record for a setup. Failures carry the same detail as successes.",
)
