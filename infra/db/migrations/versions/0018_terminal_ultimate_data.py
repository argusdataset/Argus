"""analyst, governance and holdings data for the Terminal

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-05

Four tables for the FMP Ultimate endpoints Module 19 now serves.
The split is by natural key rather than by endpoint — see
`infra/db/schema/terminal_data.py` for why an event stream cannot share a
key shape with a snapshot.

All four join migration 0003's append-only guard, exactly as
`canonical_news` did in 0010. A revised estimate or a moved consensus
arrives as a new row with a later `observation_time`; the earlier row
stays, because it is what ARGUS could have known at the earlier instant.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table this migration adds, in creation order. Each one joins
#: 0003's guard below.
TABLES: tuple[str, ...] = (
    "canonical_disclosures",
    "canonical_snapshots",
    "analyst_grades",
    "technical_indicators",
)


def _pit_columns() -> list[sa.Column]:
    return [
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingestion_time", sa.DateTime(timezone=True), nullable=False),
    ]


def _jsonb(name: str, *, nullable: bool = False, default: str | None = None) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=nullable,
        server_default=default,
    )


def upgrade() -> None:
    op.create_table(
        "canonical_disclosures",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("disclosure_type", sa.Text(), nullable=False),
        sa.Column("fiscal_period", sa.Text(), nullable=False),
        sa.Column("fiscal_period_end", sa.DateTime(timezone=True), nullable=True),
        *_pit_columns(),
        _jsonb("data"),
        _jsonb("lineage", default="{}"),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_canonical_disclosures_security_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "security_id",
            "disclosure_type",
            "fiscal_period",
            "observation_time",
            name="uq_disclosure_observation",
        ),
        comment=(
            "Period-keyed provider records that are not financial statements "
            "(analyst estimates, executive compensation, earnings transcripts)."
        ),
    )
    op.create_index(
        "ix_disclosures_lookup",
        "canonical_disclosures",
        ["security_id", "disclosure_type", "availability_time"],
    )

    op.create_table(
        "canonical_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_type", sa.Text(), nullable=False),
        *_pit_columns(),
        _jsonb("data"),
        _jsonb("lineage", default="{}"),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_canonical_snapshots_security_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "security_id", "snapshot_type", "observation_time", name="uq_snapshot_observation"
        ),
        comment=(
            "Rolling per-security states with no fiscal period "
            "(price-target consensus, peer group, fund holdings)."
        ),
    )
    op.create_index(
        "ix_snapshots_lookup",
        "canonical_snapshots",
        ["security_id", "snapshot_type", "availability_time"],
    )

    op.create_table(
        "analyst_grades",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        *_pit_columns(),
        sa.Column("grading_company", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("previous_grade", sa.Text(), nullable=True),
        sa.Column("new_grade", sa.Text(), nullable=True),
        _jsonb("data"),
        _jsonb("lineage", default="{}"),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_analyst_grades_security_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "security_id",
            "grading_company",
            "event_time",
            "new_grade",
            name="uq_analyst_grade_action",
        ),
        comment="Analyst rating changes, one row per firm per action. An event stream.",
    )
    op.create_index(
        "ix_analyst_grades_lookup", "analyst_grades", ["security_id", "availability_time"]
    )

    op.create_table(
        "technical_indicators",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("indicator", sa.Text(), nullable=False),
        sa.Column("period_length", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        *_pit_columns(),
        sa.Column("value", sa.Numeric(), nullable=True),
        _jsonb("data"),
        _jsonb("lineage", default="{}"),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_technical_indicators_security_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "security_id",
            "indicator",
            "period_length",
            "timeframe",
            "event_time",
            name="uq_technical_indicator_point",
        ),
        comment="Provider-computed technical indicator series. Never recomputed by ARGUS.",
    )
    op.create_index(
        "ix_technical_indicators_series",
        "technical_indicators",
        ["security_id", "indicator", "period_length", "timeframe", "event_time"],
    )
    op.create_index(
        "ix_technical_indicators_availability",
        "technical_indicators",
        ["availability_time", "security_id"],
    )

    # Join the guard migration 0003 applies to every other canonical
    # table. `argus_reject_mutation()` already exists; these tables just
    # opt into it, the same way canonical_news did in 0010.
    for table in TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION argus_reject_mutation();
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_truncate
            BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION argus_reject_mutation();
            """
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")

    op.drop_index("ix_technical_indicators_availability", table_name="technical_indicators")
    op.drop_index("ix_technical_indicators_series", table_name="technical_indicators")
    op.drop_table("technical_indicators")
    op.drop_index("ix_analyst_grades_lookup", table_name="analyst_grades")
    op.drop_table("analyst_grades")
    op.drop_index("ix_snapshots_lookup", table_name="canonical_snapshots")
    op.drop_table("canonical_snapshots")
    op.drop_index("ix_disclosures_lookup", table_name="canonical_disclosures")
    op.drop_table("canonical_disclosures")
