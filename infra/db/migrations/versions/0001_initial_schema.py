"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-22 01:50:34.541957
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Enum types -----------------------------------------------------------
# Created once, up front. Several enums (notably market_state_enum) are used
# by more than one table, so leaving creation to the per-column definitions
# would attempt CREATE TYPE repeatedly and fail on the second table.
ENUM_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("analogue_scope", ("CROSS_ASSET", "SAME_ASSET")),
    (
        "corporate_action_type",
        ("SPLIT", "DIVIDEND", "MERGER", "DELISTING", "BANKRUPTCY", "TICKER_CHANGE"),
    ),
    (
        "eligibility_gate",
        (
            "DATA_HISTORY",
            "DATA_QUALITY",
            "LIQUIDITY",
            "BANKRUPTCY_RISK",
            "MINIMUM_HISTORICAL_ANALOGUES",
            "VALID_ASSET_IDENTITY",
        ),
    ),
    ("evidence_status", ("SCORED", "INSUFFICIENT_EVIDENCE")),
    ("false_positive_type", ("A", "B", "C", "D", "E", "F", "G")),
    ("historical_scan_status_enum", ("PENDING_REVIEW", "APPROVED", "REJECTED")),
    ("listing_status", ("LISTED", "SUSPENDED", "DELISTED", "BANKRUPT")),
    (
        "market_state_enum",
        (
            "DOWN_TREND",
            "BASE_FORMING",
            "CONSOLIDATION",
            "ACCUMULATION",
            "BREAKOUT_WATCH",
            "BREAKOUT_READY",
            "UPTREND",
            "DISTRIBUTION",
        ),
    ),
    ("outcome_status", ("SUCCESS", "FAILED", "EXPIRED", "INVALIDATED", "NO_VALID_OUTCOME")),
    ("review_confidence", ("HIGH", "MEDIUM", "LOW")),
    ("setup_lifecycle_status", ("DETECTION", "QUALIFICATION", "ACTIVE", "OUTCOME")),
    ("timeframe", ("H4", "DAILY", "WEEKLY", "MONTHLY")),
    ("validation_run_status", ("PENDING", "RUNNING", "COMPLETED", "FAILED")),
)

# --- Immutability guards --------------------------------------------------
# Frozen copies of infra.db.append_only as of this migration. Migrations are
# historical records and must not change behaviour when that module is later
# edited, so the lists are duplicated here deliberately rather than imported.
# tests/integration/db/test_append_only.py asserts the guards actually
# installed in the database still match that module, so drift is caught.
GUARD_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION argus_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'Table % is protected: % is not permitted. '
        'ARGUS history is append-only by design.',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;
"""

APPEND_ONLY_TABLES: tuple[str, ...] = (
    "universe_version",
    "universe_membership",
    "feature_schema_version",
    "target_model_version",
    "scoring_configuration",
    "detection_configuration",
    "data_snapshot",
    "market_state_transitions",
    "eligibility_check_results",
    "signals",
    "setup_events",
    "historical_similarity_results",
    "historical_scan_status",
    "audit_log",
)

NO_DELETE_TABLES: tuple[str, ...] = ("setups", "setup_outcomes")


def _create_enum_types() -> None:
    bind = op.get_bind()
    for name, values in ENUM_TYPES:
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)


def _drop_enum_types() -> None:
    bind = op.get_bind()
    for name, _values in ENUM_TYPES:
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)


def _install_guards() -> None:
    op.execute(GUARD_FUNCTION_SQL)
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION argus_reject_mutation();"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION argus_reject_mutation();"
        )
    for table in NO_DELETE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION argus_reject_mutation();"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION argus_reject_mutation();"
        )


def _remove_guards() -> None:
    # Dropped before the tables themselves so DROP TABLE is not blocked.
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table};")
    for table in NO_DELETE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table};")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table};")
    op.execute("DROP FUNCTION IF EXISTS argus_reject_mutation();")


def upgrade() -> None:
    _create_enum_types()
    op.create_table(
        "data_snapshot",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=False),
        sa.Column("as_of_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_checksum", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_snapshot")),
        sa.UniqueConstraint("version_label", name=op.f("uq_data_snapshot_version_label")),
        comment="Immutable point-in-time data cutoff a computation ran against.",
    )
    op.create_index("ix_data_snapshot_as_of", "data_snapshot", ["as_of_time"], unique=False)
    op.create_table(
        "detection_configuration",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_checksum", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_detection_configuration")),
        sa.UniqueConstraint("version_label", name="uq_detection_configuration_label"),
        comment="Immutable candidate-detection and eligibility-gate configurations (Module 09).",
    )
    op.create_table(
        "entitlements",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entitlements")),
        sa.UniqueConstraint("code", name=op.f("uq_entitlements_code")),
        comment="Entitlement definitions. Structure only — no logic wired.",
    )
    op.create_table(
        "feature_schema_version",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_checksum", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_schema_version")),
        sa.UniqueConstraint("version_label", name="uq_feature_schema_version_label"),
        comment="Immutable feature-vector schema versions (Module 08 publishes these).",
    )
    op.create_table(
        "roles",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("name", name=op.f("uq_roles_name")),
        comment="RBAC roles. Module 22 wires the permission logic.",
    )
    op.create_table(
        "scoring_configuration",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_checksum", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scoring_configuration")),
        sa.UniqueConstraint("version_label", name="uq_scoring_configuration_label"),
        comment="Immutable scoring configurations: component weights etc. (Module 13).",
    )
    op.create_table(
        "security_identity",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("figi", sa.Text(), nullable=True),
        sa.Column("cik", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_identity")),
        sa.UniqueConstraint("figi", name=op.f("uq_security_identity_figi")),
        comment="Stable internal identity for a security. Referenced by everything; never the ticker.",
    )
    op.create_table(
        "target_model_version",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_checksum", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_target_model_version")),
        sa.UniqueConstraint("version_label", name="uq_target_model_version_label"),
        comment="Immutable target-model versions, e.g. target-model-v1 (Module 10).",
    )
    op.create_table(
        "universe_version",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("as_of_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_universe_version")),
        sa.UniqueConstraint("version_label", name=op.f("uq_universe_version_version_label")),
        comment="Immutable versioned definition of the analyzable universe.",
    )
    op.create_table(
        "canonical_corporate_actions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column(
            "action_type",
            postgresql.ENUM(
                "SPLIT",
                "DIVIDEND",
                "MERGER",
                "DELISTING",
                "BANKRUPTCY",
                "TICKER_CHANGE",
                name="corporate_action_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingestion_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_canonical_corporate_actions_security_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_canonical_corporate_actions")),
        sa.UniqueConstraint(
            "security_id",
            "action_type",
            "effective_date",
            "observation_time",
            name="uq_corporate_action_observation",
        ),
        comment="Splits, dividends, mergers, delistings, bankruptcies, ticker changes.",
    )
    op.create_index(
        "ix_corporate_actions_availability",
        "canonical_corporate_actions",
        ["availability_time", "security_id"],
        unique=False,
    )
    op.create_index(
        "ix_corporate_actions_security",
        "canonical_corporate_actions",
        ["security_id", "effective_date"],
        unique=False,
    )
    op.create_table(
        "canonical_fundamentals",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("statement_type", sa.Text(), nullable=False),
        sa.Column("fiscal_period", sa.Text(), nullable=False),
        sa.Column("fiscal_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingestion_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("restates_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["restates_id"],
            ["canonical_fundamentals.id"],
            name=op.f("fk_canonical_fundamentals_restates_id"),
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_canonical_fundamentals_security_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_canonical_fundamentals")),
        sa.UniqueConstraint(
            "security_id",
            "statement_type",
            "fiscal_period",
            "observation_time",
            name="uq_fundamentals_observation",
        ),
        comment="Canonical fundamentals. Restatements arrive as new rows, never edits.",
    )
    op.create_index(
        "ix_fundamentals_availability",
        "canonical_fundamentals",
        ["availability_time", "security_id"],
        unique=False,
    )
    op.create_index(
        "ix_fundamentals_data",
        "canonical_fundamentals",
        ["data"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_fundamentals_security_period",
        "canonical_fundamentals",
        ["security_id", "fiscal_period_end"],
        unique=False,
    )
    op.create_table(
        "canonical_ohlcv",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column(
            "timeframe",
            postgresql.ENUM(
                "H4", "DAILY", "WEEKLY", "MONTHLY", name="timeframe", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingestion_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_raw", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("high_raw", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("low_raw", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("close_raw", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("volume_raw", sa.BigInteger(), nullable=False),
        sa.Column("open_adjusted", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("high_adjusted", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("low_adjusted", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("close_adjusted", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("volume_adjusted", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("high_raw >= low_raw", name=op.f("ck_canonical_ohlcv_high_ge_low")),
        sa.CheckConstraint("volume_raw >= 0", name=op.f("ck_canonical_ohlcv_volume_non_negative")),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_canonical_ohlcv_security_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_canonical_ohlcv")),
        sa.UniqueConstraint(
            "security_id",
            "timeframe",
            "event_time",
            "observation_time",
            name="uq_ohlcv_observation",
        ),
        comment="Canonical OHLCV bars, raw and adjusted, with full PIT timestamps.",
    )
    op.create_index(
        "ix_ohlcv_availability",
        "canonical_ohlcv",
        ["availability_time", "security_id", "timeframe"],
        unique=False,
    )
    op.create_index(
        "ix_ohlcv_security_time",
        "canonical_ohlcv",
        ["security_id", "timeframe", "event_time"],
        unique=False,
    )
    op.create_table(
        "eligibility_check_results",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column(
            "gate",
            postgresql.ENUM(
                "DATA_HISTORY",
                "DATA_QUALITY",
                "LIQUIDITY",
                "BANKRUPTCY_RISK",
                "MINIMUM_HISTORICAL_ANALOGUES",
                "VALID_ASSET_IDENTITY",
                name="eligibility_gate",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("detection_configuration_id", sa.UUID(), nullable=False),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["detection_configuration_id"],
            ["detection_configuration.id"],
            name=op.f("fk_eligibility_check_results_detection_configuration_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_eligibility_check_results_security_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eligibility_check_results")),
        sa.UniqueConstraint("run_id", "security_id", "gate", name="uq_eligibility_gate_result"),
        comment="Pass/fail per eligibility gate, per candidate, per detection run.",
    )
    op.create_index(
        "ix_eligibility_run", "eligibility_check_results", ["run_id", "passed"], unique=False
    )
    op.create_index(
        "ix_eligibility_security",
        "eligibility_check_results",
        ["security_id", "evaluated_at"],
        unique=False,
    )
    op.create_table(
        "feature_vectors",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("feature_schema_version_id", sa.UUID(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["feature_schema_version_id"],
            ["feature_schema_version.id"],
            name=op.f("fk_feature_vectors_feature_schema_version_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_feature_vectors_security_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_vectors")),
        sa.UniqueConstraint(
            "security_id", "feature_schema_version_id", "event_time", name="uq_feature_vector"
        ),
        comment="Computed feature vectors, stamped with the schema version that defined them.",
    )
    op.create_index(
        "ix_feature_vectors_availability",
        "feature_vectors",
        ["availability_time", "security_id"],
        unique=False,
    )
    op.create_index(
        "ix_feature_vectors_schema",
        "feature_vectors",
        ["feature_schema_version_id", "event_time"],
        unique=False,
    )
    op.create_index(
        "ix_feature_vectors_security_time",
        "feature_vectors",
        ["security_id", "event_time"],
        unique=False,
    )
    op.create_table(
        "historical_similarity_results",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "scope",
            postgresql.ENUM("CROSS_ASSET", "SAME_ASSET", name="analogue_scope", create_type=False),
            nullable=False,
        ),
        sa.Column("similar_setup_count", sa.Integer(), nullable=False),
        sa.Column(
            "similarity_distribution", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("median_outcome", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("average_outcome", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("mfe_distribution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("mae_distribution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure_rate", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("expansion_magnitude", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("time_to_expansion", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("outcome_by_regime", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("feature_schema_version_id", sa.UUID(), nullable=False),
        sa.Column("data_snapshot_id", sa.UUID(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "failure_rate IS NULL OR (failure_rate >= 0 AND failure_rate <= 1)",
            name=op.f("ck_historical_similarity_results_failure_rate_in_range"),
        ),
        sa.CheckConstraint(
            "similar_setup_count >= 0",
            name=op.f("ck_historical_similarity_results_setup_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["data_snapshot_id"],
            ["data_snapshot.id"],
            name=op.f("fk_historical_similarity_results_data_snapshot_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["feature_schema_version_id"],
            ["feature_schema_version.id"],
            name=op.f("fk_historical_similarity_results_feature_schema_version_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_historical_similarity_results_security_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_historical_similarity_results")),
        sa.UniqueConstraint(
            "security_id", "event_time", "scope", "data_snapshot_id", name="uq_similarity_scope"
        ),
        comment="Historical analogue statistics, cross-asset and same-asset kept separate.",
    )
    op.create_index(
        "ix_similarity_security_time",
        "historical_similarity_results",
        ["security_id", "event_time"],
        unique=False,
    )
    op.create_table(
        "market_state",
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(
                "DOWN_TREND",
                "BASE_FORMING",
                "CONSOLIDATION",
                "ACCUMULATION",
                "BREAKOUT_WATCH",
                "BREAKOUT_READY",
                "UPTREND",
                "DISTRIBUTION",
                name="market_state_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("target_model_version_id", sa.UUID(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 100)",
            name=op.f("ck_market_state_confidence_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_market_state_security_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_model_version_id"],
            ["target_model_version.id"],
            name=op.f("fk_market_state_target_model_version_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("security_id", name=op.f("pk_market_state")),
        comment="Current market state per security. A derived projection — market_state_transitions is the append-only authority.",
    )
    op.create_index("ix_market_state_state", "market_state", ["state"], unique=False)
    op.create_table(
        "market_state_transitions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column(
            "from_state",
            postgresql.ENUM(
                "DOWN_TREND",
                "BASE_FORMING",
                "CONSOLIDATION",
                "ACCUMULATION",
                "BREAKOUT_WATCH",
                "BREAKOUT_READY",
                "UPTREND",
                "DISTRIBUTION",
                name="market_state_enum",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "to_state",
            postgresql.ENUM(
                "DOWN_TREND",
                "BASE_FORMING",
                "CONSOLIDATION",
                "ACCUMULATION",
                "BREAKOUT_WATCH",
                "BREAKOUT_READY",
                "UPTREND",
                "DISTRIBUTION",
                name="market_state_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("transition_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_in_prior_state", sa.Interval(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_model_version_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 100)",
            name=op.f("ck_market_state_transitions_confidence_in_range"),
        ),
        sa.CheckConstraint(
            "from_state IS NULL OR duration_in_prior_state IS NOT NULL",
            name=op.f("ck_market_state_transitions_prior_duration_required"),
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_market_state_transitions_security_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_model_version_id"],
            ["target_model_version.id"],
            name=op.f("fk_market_state_transitions_target_model_version_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_state_transitions")),
        comment="Append-only market state transition history. Backward transitions are expected.",
    )
    op.create_index(
        "ix_state_transitions_security_time",
        "market_state_transitions",
        ["security_id", "transition_time"],
        unique=False,
    )
    op.create_index(
        "ix_state_transitions_to_state",
        "market_state_transitions",
        ["to_state", "transition_time"],
        unique=False,
    )
    op.create_table(
        "model_validation_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("target_model_version_id", sa.UUID(), nullable=False),
        sa.Column("feature_schema_version_id", sa.UUID(), nullable=False),
        sa.Column("scoring_configuration_id", sa.UUID(), nullable=False),
        sa.Column("detection_configuration_id", sa.UUID(), nullable=False),
        sa.Column("universe_version_id", sa.UUID(), nullable=False),
        sa.Column("data_snapshot_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDING",
                "RUNNING",
                "COMPLETED",
                "FAILED",
                name="validation_run_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["data_snapshot_id"],
            ["data_snapshot.id"],
            name=op.f("fk_model_validation_runs_data_snapshot_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["detection_configuration_id"],
            ["detection_configuration.id"],
            name=op.f("fk_model_validation_runs_detection_configuration_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["feature_schema_version_id"],
            ["feature_schema_version.id"],
            name=op.f("fk_model_validation_runs_feature_schema_version_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_configuration_id"],
            ["scoring_configuration.id"],
            name=op.f("fk_model_validation_runs_scoring_configuration_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_model_version_id"],
            ["target_model_version.id"],
            name=op.f("fk_model_validation_runs_target_model_version_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["universe_version_id"],
            ["universe_version.id"],
            name=op.f("fk_model_validation_runs_universe_version_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_validation_runs")),
        comment="A model validation run over PIT-correct historical data.",
    )
    op.create_index(
        "ix_validation_runs_model",
        "model_validation_runs",
        ["target_model_version_id", "started_at"],
        unique=False,
    )
    op.create_table(
        "pending_material_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_binary", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingestion_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_pending_material_events_security_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_material_events")),
        sa.UniqueConstraint(
            "security_id",
            "event_type",
            "scheduled_for",
            "observation_time",
            name="uq_material_event_observation",
        ),
        comment="Upcoming binary/material events. Type is free text so new kinds need no migration.",
    )
    op.create_index(
        "ix_material_events_availability",
        "pending_material_events",
        ["availability_time", "security_id"],
        unique=False,
    )
    op.create_index(
        "ix_material_events_schedule",
        "pending_material_events",
        ["scheduled_for", "security_id"],
        unique=False,
    )
    op.create_table(
        "security_ticker_history",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name=op.f("ck_security_ticker_history_valid_range_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_security_ticker_history_security_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_ticker_history")),
        sa.UniqueConstraint("security_id", "ticker", "valid_from", name="uq_ticker_validity"),
        comment="Ticker <-> identity mapping over time. Tickers change; identity does not.",
    )
    op.create_index(
        "ix_ticker_history_lookup",
        "security_ticker_history",
        ["ticker", "valid_from", "valid_to"],
        unique=False,
    )
    op.create_index(
        "ix_ticker_history_security",
        "security_ticker_history",
        ["security_id", "valid_from"],
        unique=False,
    )
    op.create_table(
        "setups",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_model_version_id", sa.UUID(), nullable=False),
        sa.Column("detection_configuration_id", sa.UUID(), nullable=False),
        sa.Column("universe_version_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["detection_configuration_id"],
            ["detection_configuration.id"],
            name=op.f("fk_setups_detection_configuration_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_setups_security_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_model_version_id"],
            ["target_model_version.id"],
            name=op.f("fk_setups_target_model_version_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["universe_version_id"],
            ["universe_version.id"],
            name=op.f("fk_setups_universe_version_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_setups")),
        comment="A detected setup. Current lifecycle status is derived from setup_events.",
    )
    op.create_index(
        "ix_setups_security_time", "setups", ["security_id", "detected_at"], unique=False
    )
    op.create_table(
        "signals",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "evidence_status",
            postgresql.ENUM(
                "SCORED", "INSUFFICIENT_EVIDENCE", name="evidence_status", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("argus_score", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("opportunity_score", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("risk_score", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("probability", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("probability_definition", sa.Text(), nullable=True),
        sa.Column("component_pattern_quality", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("component_historical_evidence", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("component_market_regime", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("component_volume_liquidity", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column(
            "component_volatility_structure", sa.Numeric(precision=6, scale=3), nullable=True
        ),
        sa.Column("component_fundamental_context", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("component_risk_reward", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("target_model_version_id", sa.UUID(), nullable=False),
        sa.Column("feature_schema_version_id", sa.UUID(), nullable=False),
        sa.Column("data_snapshot_id", sa.UUID(), nullable=False),
        sa.Column("scoring_configuration_id", sa.UUID(), nullable=False),
        sa.Column("universe_version_id", sa.UUID(), nullable=False),
        sa.Column("detection_configuration_id", sa.UUID(), nullable=False),
        sa.Column("supersedes_signal_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "\n        (evidence_status = 'SCORED'\n            AND argus_score IS NOT NULL\n            AND confidence IS NOT NULL\n            AND opportunity_score IS NOT NULL\n            AND risk_score IS NOT NULL)\n        OR\n        (evidence_status = 'INSUFFICIENT_EVIDENCE'\n            AND argus_score IS NULL\n            AND confidence IS NULL\n            AND opportunity_score IS NULL\n            AND risk_score IS NULL\n            AND probability IS NULL)\n        ",
            name=op.f("ck_signals_scores_match_evidence_status"),
        ),
        sa.CheckConstraint(
            "argus_score IS NULL OR (argus_score >= 0 AND argus_score <= 100)",
            name=op.f("ck_signals_argus_score_in_range"),
        ),
        sa.CheckConstraint(
            "component_fundamental_context IS NULL OR (component_fundamental_context >= 0 AND component_fundamental_context <= 100)",
            name=op.f("ck_signals_component_fundamental_context_in_range"),
        ),
        sa.CheckConstraint(
            "component_historical_evidence IS NULL OR (component_historical_evidence >= 0 AND component_historical_evidence <= 100)",
            name=op.f("ck_signals_component_historical_evidence_in_range"),
        ),
        sa.CheckConstraint(
            "component_market_regime IS NULL OR (component_market_regime >= 0 AND component_market_regime <= 100)",
            name=op.f("ck_signals_component_market_regime_in_range"),
        ),
        sa.CheckConstraint(
            "component_pattern_quality IS NULL OR (component_pattern_quality >= 0 AND component_pattern_quality <= 100)",
            name=op.f("ck_signals_component_pattern_quality_in_range"),
        ),
        sa.CheckConstraint(
            "component_risk_reward IS NULL OR (component_risk_reward >= 0 AND component_risk_reward <= 100)",
            name=op.f("ck_signals_component_risk_reward_in_range"),
        ),
        sa.CheckConstraint(
            "component_volatility_structure IS NULL OR (component_volatility_structure >= 0 AND component_volatility_structure <= 100)",
            name=op.f("ck_signals_component_volatility_structure_in_range"),
        ),
        sa.CheckConstraint(
            "component_volume_liquidity IS NULL OR (component_volume_liquidity >= 0 AND component_volume_liquidity <= 100)",
            name=op.f("ck_signals_component_volume_liquidity_in_range"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 100)",
            name=op.f("ck_signals_confidence_in_range"),
        ),
        sa.CheckConstraint(
            "opportunity_score IS NULL OR (opportunity_score >= 0 AND opportunity_score <= 100)",
            name=op.f("ck_signals_opportunity_score_in_range"),
        ),
        sa.CheckConstraint(
            "probability IS NULL OR (probability >= 0 AND probability <= 100)",
            name=op.f("ck_signals_probability_in_range"),
        ),
        sa.CheckConstraint(
            "probability IS NULL OR probability_definition IS NOT NULL",
            name=op.f("ck_signals_probability_requires_definition"),
        ),
        sa.CheckConstraint(
            "risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100)",
            name=op.f("ck_signals_risk_score_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["data_snapshot_id"],
            ["data_snapshot.id"],
            name=op.f("fk_signals_data_snapshot_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["detection_configuration_id"],
            ["detection_configuration.id"],
            name=op.f("fk_signals_detection_configuration_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["feature_schema_version_id"],
            ["feature_schema_version.id"],
            name=op.f("fk_signals_feature_schema_version_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_configuration_id"],
            ["scoring_configuration.id"],
            name=op.f("fk_signals_scoring_configuration_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_signals_security_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_signal_id"], ["signals.id"], name=op.f("fk_signals_supersedes_signal_id")
        ),
        sa.ForeignKeyConstraint(
            ["target_model_version_id"],
            ["target_model_version.id"],
            name=op.f("fk_signals_target_model_version_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["universe_version_id"],
            ["universe_version.id"],
            name=op.f("fk_signals_universe_version_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signals")),
        comment="Immutable scored signals with full lineage. Corrections supersede, never edit.",
    )
    op.create_index(
        "ix_signals_ranking", "signals", ["evidence_status", "argus_score"], unique=False
    )
    op.create_index(
        "ix_signals_security_time", "signals", ["security_id", "event_time"], unique=False
    )
    op.create_index("ix_signals_snapshot", "signals", ["data_snapshot_id"], unique=False)
    op.create_table(
        "universe_membership",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("universe_version_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column(
            "listing_status",
            postgresql.ENUM(
                "LISTED",
                "SUSPENDED",
                "DELISTED",
                "BANKRUPT",
                name="listing_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("has_sufficient_history", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_universe_membership_security_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["universe_version_id"],
            ["universe_version.id"],
            name=op.f("fk_universe_membership_universe_version_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_universe_membership")),
        sa.UniqueConstraint("universe_version_id", "security_id", name="uq_membership"),
        comment="Which securities were in which universe version, including delisted/bankrupt ones.",
    )
    op.create_index(
        "ix_universe_membership_security", "universe_membership", ["security_id"], unique=False
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("mfa_secret", sa.Text(), nullable=True),
        sa.Column("mfa_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_users_role_id"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        comment="User accounts. Structure only — authentication logic is Module 22.",
    )
    op.create_index("ix_users_role", "users", ["role_id"], unique=False)
    op.create_table(
        "audit_log",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_log_actor_user_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        comment="Append-only audit trail.",
    )
    op.create_index(
        "ix_audit_log_actor", "audit_log", ["actor_user_id", "occurred_at"], unique=False
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"], unique=False)
    op.create_index("ix_audit_log_occurred", "audit_log", ["occurred_at"], unique=False)
    op.create_table(
        "historical_scan_status",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("model_validation_run_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDING_REVIEW",
                "APPROVED",
                "REJECTED",
                name="historical_scan_status_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("assigned_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"],
            ["users.id"],
            name=op.f("fk_historical_scan_status_assigned_by_user_id"),
        ),
        sa.ForeignKeyConstraint(
            ["model_validation_run_id"],
            ["model_validation_runs.id"],
            name=op.f("fk_historical_scan_status_model_validation_run_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_historical_scan_status")),
        comment="Append-only review-gate history. Current status is the latest row per run.",
    )
    op.create_index(
        "ix_scan_status_run",
        "historical_scan_status",
        ["model_validation_run_id", "assigned_at"],
        unique=False,
    )
    op.create_table(
        "model_evaluation_reports",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("model_validation_run_id", sa.UUID(), nullable=False),
        sa.Column("precision", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("recall", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("hit_rate", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("false_positive_rate", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("expectancy", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "hit_rate IS NULL OR (hit_rate >= 0 AND hit_rate <= 1)",
            name=op.f("ck_model_evaluation_reports_hit_rate_in_range"),
        ),
        sa.CheckConstraint(
            "precision IS NULL OR (precision >= 0 AND precision <= 1)",
            name=op.f("ck_model_evaluation_reports_precision_in_range"),
        ),
        sa.CheckConstraint(
            "recall IS NULL OR (recall >= 0 AND recall <= 1)",
            name=op.f("ck_model_evaluation_reports_recall_in_range"),
        ),
        sa.CheckConstraint(
            "sample_size >= 0", name=op.f("ck_model_evaluation_reports_sample_size_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["model_validation_run_id"],
            ["model_validation_runs.id"],
            name=op.f("fk_model_evaluation_reports_model_validation_run_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_evaluation_reports")),
        comment="Metrics derived from a validation run's predictions and actual outcomes.",
    )
    op.create_index(
        "ix_evaluation_reports_run",
        "model_evaluation_reports",
        ["model_validation_run_id"],
        unique=False,
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "expires_at > issued_at", name=op.f("ck_sessions_session_expiry_after_issue")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
        comment="Authentication sessions. Token hashes only.",
    )
    op.create_index("ix_sessions_user", "sessions", ["user_id", "expires_at"], unique=False)
    op.create_table(
        "setup_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("setup_id", sa.UUID(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column(
            "lifecycle_status",
            postgresql.ENUM(
                "DETECTION",
                "QUALIFICATION",
                "ACTIVE",
                "OUTCOME",
                name="setup_lifecycle_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence_number >= 0", name=op.f("ck_setup_events_sequence_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["setup_id"], ["setups.id"], name=op.f("fk_setup_events_setup_id"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_setup_events")),
        sa.UniqueConstraint("setup_id", "sequence_number", name="uq_setup_event_sequence"),
        comment="Append-only setup lifecycle events. The source of truth for setup status.",
    )
    op.create_index(
        "ix_setup_events_setup", "setup_events", ["setup_id", "sequence_number"], unique=False
    )
    op.create_table(
        "setup_outcomes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("setup_id", sa.UUID(), nullable=False),
        sa.Column(
            "outcome_status",
            postgresql.ENUM(
                "SUCCESS",
                "FAILED",
                "EXPIRED",
                "INVALIDATED",
                "NO_VALID_OUTCOME",
                name="outcome_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("mfe", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("mae", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("time_to_mfe", sa.Interval(), nullable=True),
        sa.Column("time_to_mae", sa.Interval(), nullable=True),
        sa.Column("outcome_window", sa.Interval(), nullable=True),
        sa.Column("realized_return", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("benchmark_relative_return", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("volatility_adjusted_outcome", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            "market_regime_at_outcome",
            postgresql.ENUM(
                "DOWN_TREND",
                "BASE_FORMING",
                "CONSOLIDATION",
                "ACCUMULATION",
                "BREAKOUT_WATCH",
                "BREAKOUT_READY",
                "UPTREND",
                "DISTRIBUTION",
                name="market_state_enum",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "review_confidence",
            postgresql.ENUM("HIGH", "MEDIUM", "LOW", name="review_confidence", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "false_positive_type",
            postgresql.ENUM(
                "A", "B", "C", "D", "E", "F", "G", name="false_positive_type", create_type=False
            ),
            nullable=True,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["setup_id"],
            ["setups.id"],
            name=op.f("fk_setup_outcomes_setup_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_setup_outcomes")),
        sa.UniqueConstraint("setup_id", name=op.f("uq_setup_outcomes_setup_id")),
        comment="Outcome and case record for a setup. Failures carry the same detail as successes.",
    )
    op.create_index(
        "ix_setup_outcomes_false_positive", "setup_outcomes", ["false_positive_type"], unique=False
    )
    op.create_index("ix_setup_outcomes_status", "setup_outcomes", ["outcome_status"], unique=False)
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("entitlement_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["entitlement_id"],
            ["entitlements.id"],
            name=op.f("fk_subscriptions_entitlement_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_subscriptions_user_id"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
        comment="Subscription records. Structure only — no billing logic wired.",
    )
    op.create_index("ix_subscriptions_user", "subscriptions", ["user_id", "status"], unique=False)
    op.create_table(
        "user_watchlists",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_watchlists_user_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_watchlists")),
        sa.UniqueConstraint("user_id", "name", name="uq_user_watchlist_name"),
        comment="User-created watchlists. Distinct from ARGUS Intelligence derived watchlists.",
    )
    op.create_table(
        "user_watchlist_items",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("watchlist_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_user_watchlist_items_security_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["watchlist_id"],
            ["user_watchlists.id"],
            name=op.f("fk_user_watchlist_items_watchlist_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_watchlist_items")),
        sa.UniqueConstraint("watchlist_id", "security_id", name="uq_watchlist_item"),
        comment="Securities on a user watchlist.",
    )
    op.create_index(
        "ix_watchlist_items_watchlist", "user_watchlist_items", ["watchlist_id"], unique=False
    )
    _install_guards()


def downgrade() -> None:
    _remove_guards()
    op.drop_index("ix_watchlist_items_watchlist", table_name="user_watchlist_items")
    op.drop_table("user_watchlist_items")
    op.drop_table("user_watchlists")
    op.drop_index("ix_subscriptions_user", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index("ix_setup_outcomes_status", table_name="setup_outcomes")
    op.drop_index("ix_setup_outcomes_false_positive", table_name="setup_outcomes")
    op.drop_table("setup_outcomes")
    op.drop_index("ix_setup_events_setup", table_name="setup_events")
    op.drop_table("setup_events")
    op.drop_index("ix_sessions_user", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_evaluation_reports_run", table_name="model_evaluation_reports")
    op.drop_table("model_evaluation_reports")
    op.drop_index("ix_scan_status_run", table_name="historical_scan_status")
    op.drop_table("historical_scan_status")
    op.drop_index("ix_audit_log_occurred", table_name="audit_log")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_index("ix_audit_log_actor", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_users_role", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_universe_membership_security", table_name="universe_membership")
    op.drop_table("universe_membership")
    op.drop_index("ix_signals_snapshot", table_name="signals")
    op.drop_index("ix_signals_security_time", table_name="signals")
    op.drop_index("ix_signals_ranking", table_name="signals")
    op.drop_table("signals")
    op.drop_index("ix_setups_security_time", table_name="setups")
    op.drop_table("setups")
    op.drop_index("ix_ticker_history_security", table_name="security_ticker_history")
    op.drop_index("ix_ticker_history_lookup", table_name="security_ticker_history")
    op.drop_table("security_ticker_history")
    op.drop_index("ix_material_events_schedule", table_name="pending_material_events")
    op.drop_index("ix_material_events_availability", table_name="pending_material_events")
    op.drop_table("pending_material_events")
    op.drop_index("ix_validation_runs_model", table_name="model_validation_runs")
    op.drop_table("model_validation_runs")
    op.drop_index("ix_state_transitions_to_state", table_name="market_state_transitions")
    op.drop_index("ix_state_transitions_security_time", table_name="market_state_transitions")
    op.drop_table("market_state_transitions")
    op.drop_index("ix_market_state_state", table_name="market_state")
    op.drop_table("market_state")
    op.drop_index("ix_similarity_security_time", table_name="historical_similarity_results")
    op.drop_table("historical_similarity_results")
    op.drop_index("ix_feature_vectors_security_time", table_name="feature_vectors")
    op.drop_index("ix_feature_vectors_schema", table_name="feature_vectors")
    op.drop_index("ix_feature_vectors_availability", table_name="feature_vectors")
    op.drop_table("feature_vectors")
    op.drop_index("ix_eligibility_security", table_name="eligibility_check_results")
    op.drop_index("ix_eligibility_run", table_name="eligibility_check_results")
    op.drop_table("eligibility_check_results")
    op.drop_index("ix_ohlcv_security_time", table_name="canonical_ohlcv")
    op.drop_index("ix_ohlcv_availability", table_name="canonical_ohlcv")
    op.drop_table("canonical_ohlcv")
    op.drop_index("ix_fundamentals_security_period", table_name="canonical_fundamentals")
    op.drop_index(
        "ix_fundamentals_data", table_name="canonical_fundamentals", postgresql_using="gin"
    )
    op.drop_index("ix_fundamentals_availability", table_name="canonical_fundamentals")
    op.drop_table("canonical_fundamentals")
    op.drop_index("ix_corporate_actions_security", table_name="canonical_corporate_actions")
    op.drop_index("ix_corporate_actions_availability", table_name="canonical_corporate_actions")
    op.drop_table("canonical_corporate_actions")
    op.drop_table("universe_version")
    op.drop_table("target_model_version")
    op.drop_table("security_identity")
    op.drop_table("scoring_configuration")
    op.drop_table("roles")
    op.drop_table("feature_schema_version")
    op.drop_table("entitlements")
    op.drop_table("detection_configuration")
    op.drop_index("ix_data_snapshot_as_of", table_name="data_snapshot")
    op.drop_table("data_snapshot")
    _drop_enum_types()
