"""store the daily news-volume-anomaly reading, one row per security per day

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-04

Module 28 counts today's `canonical_news` rows against a trailing
baseline and decides whether volume is unusually high for that security.
This is where the decision is written down, so `services/intelligence`
can read it with a `SELECT` and never recompute it.

## Upsertable on `(security_id, signal_date)`, not append-only

A rerun of the same day overwrites that day's row — the same shape
`core/market_state/transitions.py` uses to upsert the `market_state`
projection. Not append-only guarded, for the reason migration 0009 gave
`live_scan_runs` and 0014 gave `deep_refresh_log`: an operational,
recomputable annotation, not a fact ARGUS is claiming to have believed at
a point in time in the sense Module 03's guards protect.

## `raised` is a nullable boolean, deliberately

NULL means undetermined — insufficient baseline history to say anything —
and is a different fact from `false`. Making it a nullable column rather
than, say, a non-nullable boolean plus a separate "determined" flag keeps
the tri-state representable with one column and one obvious NULL check,
matching `core/risk_context/flags.py`'s `RiskFlag.raised` exactly.

## `unavailable_reason` is text, not a second Postgres enum

Its values are drawn from `core.data_validation.result.MissReason`, a
Python enum several unrelated modules already share. Giving this table
its own native enum type would mean two places to add a value to in step
whenever that shared vocabulary grows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "news_volume_signals"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("raised", sa.Boolean(), nullable=True),
        sa.Column("today_count", sa.Integer(), nullable=False),
        sa.Column("baseline_mean", sa.Numeric(), nullable=True),
        sa.Column("baseline_window_days", sa.Integer(), nullable=False),
        sa.Column("multiple_threshold", sa.Numeric(), nullable=False),
        sa.Column("unavailable_reason", sa.Text(), nullable=True),
        sa.Column("config_version_label", sa.Text(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_news_volume_signals_security_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "security_id", "signal_date", name="uq_news_volume_signal_security_date"
        ),
        comment=(
            "One stored news-volume-anomaly reading per security per day (Module 28). "
            "raised is tri-state; NULL means undetermined, never 'not raised'."
        ),
    )
    op.create_index("ix_news_volume_signals_security", TABLE, ["security_id", "signal_date"])


def downgrade() -> None:
    op.drop_index("ix_news_volume_signals_security", table_name=TABLE)
    op.drop_table(TABLE)
