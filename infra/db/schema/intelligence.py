"""Market state, eligibility, signals, similarity and pending events.

Three things in this module are structural expressions of ARGUS rules
rather than ordinary modelling choices:

1. `market_state` holds only the *current* state and is a derived
   projection; `market_state_transitions` is the append-only authority.
   A security's state history — including backward moves, such as a
   failed breakout returning to CONSOLIDATION — is itself a feature, so
   it can never be overwritten.

2. `signals.evidence_status` keeps "we deliberately did not score this"
   distinct from "scored low". An INSUFFICIENT_EVIDENCE signal has all
   five numbers NULL (CHECK-enforced); a SCORED signal has all five
   present. "No data at all" is the absence of a row. Forcing a numeric
   score onto insufficient evidence is precisely the failure mode this
   prevents.

3. `historical_similarity_results` stores cross-asset and same-asset
   analogues as separate rows. They are independent evidence — a
   security's own history is not automatic reinforcement of a
   cross-asset pattern — so the schema never lets them be summed into a
   single blended number by accident.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
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
    AnalogueScope,
    EligibilityGate,
    EvidenceStatus,
    MarketState,
)
from infra.db.metadata import metadata, pg_enum, pit_columns

#: All five headline numbers and all seven components are 0-100.
_SCORE = Numeric(6, 3)


def _score_range_check(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} IS NULL OR ({column} >= 0 AND {column} <= 100)",
        name=f"{column}_in_range",
    )


# --------------------------------------------------------------------------
# Market state (Module 10)
# --------------------------------------------------------------------------

market_state = Table(
    "market_state",
    metadata,
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("state", pg_enum(MarketState, "market_state_enum"), nullable=False),
    Column("entered_at", DateTime(timezone=True), nullable=False),
    Column("confidence", _SCORE, nullable=True),
    Column(
        "target_model_version_id",
        UUID(as_uuid=True),
        ForeignKey("target_model_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    _score_range_check("confidence"),
    Index("ix_market_state_state", "state"),
    comment=(
        "Current market state per security. A derived projection — "
        "market_state_transitions is the append-only authority."
    ),
)

market_state_transitions = Table(
    "market_state_transitions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # NULL when a security is first classified and had no prior state.
    Column("from_state", pg_enum(MarketState, "market_state_enum"), nullable=True),
    Column("to_state", pg_enum(MarketState, "market_state_enum"), nullable=False),
    Column("transition_time", DateTime(timezone=True), nullable=False),
    # How long the security sat in from_state. NULL only for the first
    # observed transition. Duration in each state is a feature Module 08
    # reads, which is why it is stored rather than recomputed.
    Column("duration_in_prior_state", Interval, nullable=True),
    Column("confidence", _SCORE, nullable=True),
    # Measurable criteria that justified the transition.
    Column("evidence", JSONB, nullable=False),
    Column(
        "target_model_version_id",
        UUID(as_uuid=True),
        ForeignKey("target_model_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    _score_range_check("confidence"),
    CheckConstraint(
        "from_state IS NULL OR duration_in_prior_state IS NOT NULL",
        name="prior_duration_required",
    ),
    Index("ix_state_transitions_security_time", "security_id", "transition_time"),
    Index("ix_state_transitions_to_state", "to_state", "transition_time"),
    comment="Append-only market state transition history. Backward transitions are expected.",
)


# --------------------------------------------------------------------------
# Eligibility (Module 09)
# --------------------------------------------------------------------------

eligibility_check_results = Table(
    "eligibility_check_results",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    # Groups every gate result for one detection pass. Not a foreign key:
    # a detection run is not itself an entity in this schema, and
    # inventing one is Module 09's call to make, not this module's.
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("gate", pg_enum(EligibilityGate, "eligibility_gate"), nullable=False),
    Column("passed", Boolean, nullable=False),
    # Why it passed or failed — the measured value and the threshold, so
    # a rejection is explainable rather than a bare boolean.
    Column("detail", JSONB, nullable=False),
    Column(
        "detection_configuration_id",
        UUID(as_uuid=True),
        ForeignKey("detection_configuration.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("evaluated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("run_id", "security_id", "gate", name="uq_eligibility_gate_result"),
    Index("ix_eligibility_run", "run_id", "passed"),
    Index("ix_eligibility_security", "security_id", "evaluated_at"),
    comment="Pass/fail per eligibility gate, per candidate, per detection run.",
)


# --------------------------------------------------------------------------
# Signals (Module 13)
# --------------------------------------------------------------------------

signals = Table(
    "signals",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column(
        "evidence_status",
        pg_enum(EvidenceStatus, "evidence_status"),
        nullable=False,
    ),
    # --- the five distinct numbers ---
    # argus_score is composite setup quality for ranking. It is NOT a
    # probability and must never be presented as one; `probability` below
    # is the only calibrated number, and it is meaningless without its
    # definition.
    Column("argus_score", _SCORE, nullable=True),
    Column("confidence", _SCORE, nullable=True),
    Column("opportunity_score", _SCORE, nullable=True),
    Column("risk_score", _SCORE, nullable=True),
    Column("probability", _SCORE, nullable=True),
    # The predefined outcome `probability` refers to, e.g.
    # "+10% before -5% within 60 trading days". Stored alongside because
    # a probability without its definition means nothing.
    Column("probability_definition", Text, nullable=True),
    # --- the seven-component breakdown ---
    # Scores are never a single opaque number; a user can always see why.
    Column("component_pattern_quality", _SCORE, nullable=True),
    Column("component_historical_evidence", _SCORE, nullable=True),
    Column("component_market_regime", _SCORE, nullable=True),
    Column("component_volume_liquidity", _SCORE, nullable=True),
    Column("component_volatility_structure", _SCORE, nullable=True),
    Column("component_fundamental_context", _SCORE, nullable=True),
    Column("component_risk_reward", _SCORE, nullable=True),
    # --- full lineage: re-running these IDs must reproduce this row ---
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
        "data_snapshot_id",
        UUID(as_uuid=True),
        ForeignKey("data_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "scoring_configuration_id",
        UUID(as_uuid=True),
        ForeignKey("scoring_configuration.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "universe_version_id",
        UUID(as_uuid=True),
        ForeignKey("universe_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "detection_configuration_id",
        UUID(as_uuid=True),
        ForeignKey("detection_configuration.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # A correction is a new row pointing at the one it supersedes. The
    # original stays exactly as written.
    Column("supersedes_signal_id", UUID(as_uuid=True), ForeignKey("signals.id"), nullable=True),
    # The layer beneath the seven component numbers: which ramp mapped
    # which reading, why a component was unmeasured, how much of the
    # weight was measurable, the configuration's calibration status.
    # Added in migration 0005 — see it for why this table went without
    # one when every other result table has it.
    Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    _score_range_check("argus_score"),
    _score_range_check("confidence"),
    _score_range_check("opportunity_score"),
    _score_range_check("risk_score"),
    _score_range_check("probability"),
    _score_range_check("component_pattern_quality"),
    _score_range_check("component_historical_evidence"),
    _score_range_check("component_market_regime"),
    _score_range_check("component_volume_liquidity"),
    _score_range_check("component_volatility_structure"),
    _score_range_check("component_fundamental_context"),
    _score_range_check("component_risk_reward"),
    # A SCORED signal carries all five numbers; an INSUFFICIENT_EVIDENCE
    # one carries none. This is the constraint that makes "deliberately
    # not scored" structurally different from "scored low".
    CheckConstraint(
        """
        (evidence_status = 'SCORED'
            AND argus_score IS NOT NULL
            AND confidence IS NOT NULL
            AND opportunity_score IS NOT NULL
            AND risk_score IS NOT NULL)
        OR
        (evidence_status = 'INSUFFICIENT_EVIDENCE'
            AND argus_score IS NULL
            AND confidence IS NULL
            AND opportunity_score IS NULL
            AND risk_score IS NULL
            AND probability IS NULL)
        """,
        name="scores_match_evidence_status",
    ),
    # A probability is meaningless without the outcome it refers to.
    CheckConstraint(
        "probability IS NULL OR probability_definition IS NOT NULL",
        name="probability_requires_definition",
    ),
    # One scoring of one candidate: security, instant, data cutoff,
    # configuration. Partial, because a correction is deliberately a
    # second row with the same identity pointing at the one it replaces —
    # only uncorrected originals must be unique. Added in migration 0005.
    Index(
        "uq_signals_identity",
        "security_id",
        "event_time",
        "data_snapshot_id",
        "scoring_configuration_id",
        unique=True,
        postgresql_where=text("supersedes_signal_id IS NULL"),
    ),
    Index("ix_signals_security_time", "security_id", "event_time"),
    Index("ix_signals_ranking", "evidence_status", "argus_score"),
    Index("ix_signals_snapshot", "data_snapshot_id"),
    comment="Immutable scored signals with full lineage. Corrections supersede, never edit.",
)


# --------------------------------------------------------------------------
# Historical similarity (Module 11)
# --------------------------------------------------------------------------

historical_similarity_results = Table(
    "historical_similarity_results",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("event_time", DateTime(timezone=True), nullable=False),
    # Cross-asset and same-asset analogues are separate evidence and get
    # separate rows so they can never be silently blended.
    Column("scope", pg_enum(AnalogueScope, "analogue_scope"), nullable=False),
    Column("similar_setup_count", Integer, nullable=False),
    Column("similarity_distribution", JSONB, nullable=False),
    Column("median_outcome", Numeric(12, 6), nullable=True),
    Column("average_outcome", Numeric(12, 6), nullable=True),
    Column("mfe_distribution", JSONB, nullable=True),
    Column("mae_distribution", JSONB, nullable=True),
    Column("failure_rate", Numeric(8, 6), nullable=True),
    Column("expansion_magnitude", JSONB, nullable=True),
    Column("time_to_expansion", JSONB, nullable=True),
    Column("outcome_by_regime", JSONB, nullable=True),
    Column(
        "feature_schema_version_id",
        UUID(as_uuid=True),
        ForeignKey("feature_schema_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "data_snapshot_id",
        UUID(as_uuid=True),
        ForeignKey("data_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("computed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("similar_setup_count >= 0", name="setup_count_non_negative"),
    CheckConstraint(
        "failure_rate IS NULL OR (failure_rate >= 0 AND failure_rate <= 1)",
        name="failure_rate_in_range",
    ),
    UniqueConstraint(
        "security_id",
        "event_time",
        "scope",
        "data_snapshot_id",
        name="uq_similarity_scope",
    ),
    Index("ix_similarity_security_time", "security_id", "event_time"),
    comment="Historical analogue statistics, cross-asset and same-asset kept separate.",
)


# --------------------------------------------------------------------------
# Pending material events (Module 12)
# --------------------------------------------------------------------------

pending_material_events = Table(
    "pending_material_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # Deliberately free text, NOT an enum. Earnings is the only kind
    # sourced at MVP, but litigation, M&A, trial results and patent
    # decisions must be addable without a schema migration.
    Column("event_type", Text, nullable=False),
    Column("scheduled_for", DateTime(timezone=True), nullable=False),
    # Whether the outcome is binary. A structurally-driven setup and one
    # that is really speculative anticipation of a binary event look
    # similar but carry very different risk.
    Column("is_binary", Boolean, nullable=False, server_default="false"),
    # PIT timestamps matter here too: knowing *when ARGUS learned* an
    # earnings date is what stops a backtest from assuming foreknowledge
    # of a schedule that was announced later. event_time is the
    # announcement; scheduled_for is when the event will occur.
    *pit_columns(),
    Column("details", JSONB, nullable=False),
    Column("source", Text, nullable=True),
    UniqueConstraint(
        "security_id",
        "event_type",
        "scheduled_for",
        "observation_time",
        name="uq_material_event_observation",
    ),
    Index("ix_material_events_schedule", "scheduled_for", "security_id"),
    Index("ix_material_events_availability", "availability_time", "security_id"),
    comment="Upcoming binary/material events. Type is free text so new kinds need no migration.",
)
