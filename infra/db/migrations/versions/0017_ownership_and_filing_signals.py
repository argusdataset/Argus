"""sec filings, insider trades, institutional ownership, and their signals

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-04

Six tables for Vazifa 3 (FMP Ultimate plan): two raw ingested facts and
one computed signal per data family, plus 13F's raw+computed pair.

- `sec_filings` / `sec_filing_signals` — Module 28's third signal (see
  `core/news_signals/filings.py`). `raised` is NOT NULL: a same-day
  filing is a binary fact, not a tri-state anomaly.
- `insider_trades` / `insider_cluster_signals` — Module 29
  (`core/ownership_signals/insider.py`). `raised` IS nullable: an
  insider-buy cluster needs data to have been fetched at all before
  "no cluster" is a real answer rather than an assumption.
- `institutional_ownership` / `institutional_ownership_signals` — Module
  29's 13F trend (`core/ownership_signals/institutional.py`). No `raised`
  column at all — a trend, not an event.

See `infra/db/schema/sec_filings.py` and
`infra/db/schema/ownership_signals.py` for the full reasoning.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pit_columns() -> list[sa.Column]:
    return [
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingestion_time", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    # -- sec_filings / sec_filing_signals ------------------------------------
    op.create_table(
        "sec_filings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        *_pit_columns(),
        sa.Column("form_type", sa.Text(), nullable=False),
        sa.Column(
            "item_numbers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column(
            "lineage", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_sec_filings_security_id"),
            ondelete="RESTRICT",
        ),
        comment="Raw SEC filings ingested from FMP (8-K only for now). Never scored.",
    )
    op.create_index(
        "uq_sec_filings_link",
        "sec_filings",
        ["security_id", "link"],
        unique=True,
        postgresql_where=sa.text("link IS NOT NULL"),
    )
    op.create_index(
        "uq_sec_filings_no_link",
        "sec_filings",
        ["security_id", "form_type", "event_time"],
        unique=True,
        postgresql_where=sa.text("link IS NULL"),
    )
    op.create_index("ix_sec_filings_security_time", "sec_filings", ["security_id", "event_time"])
    op.create_index(
        "ix_sec_filings_availability", "sec_filings", ["availability_time", "security_id"]
    )

    op.create_table(
        "sec_filing_signals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("raised", sa.Boolean(), nullable=False),
        sa.Column(
            "item_numbers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("config_version_label", sa.Text(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_sec_filing_signals_security_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "security_id", "signal_date", name="uq_sec_filing_signal_security_date"
        ),
        comment=(
            "One same-day-8-K reading per security per day (Module 28). "
            "raised is NOT NULL — a binary fact, not a tri-state anomaly."
        ),
    )
    op.create_index(
        "ix_sec_filing_signals_security", "sec_filing_signals", ["security_id", "signal_date"]
    )

    # -- insider_trades / insider_cluster_signals ----------------------------
    op.create_table(
        "insider_trades",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        *_pit_columns(),
        sa.Column("reporting_person", sa.Text(), nullable=True),
        sa.Column("reporting_position", sa.Text(), nullable=True),
        sa.Column("transaction_code", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(), nullable=True),
        sa.Column("price", sa.Numeric(), nullable=True),
        sa.Column(
            "lineage", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_insider_trades_security_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "security_id",
            "reporting_person",
            "event_time",
            "transaction_code",
            "quantity",
            name="uq_insider_trade_natural_key",
        ),
        comment="Raw insider transactions ingested from FMP. Never scored.",
    )
    op.create_index(
        "ix_insider_trades_security_time", "insider_trades", ["security_id", "event_time"]
    )
    op.create_index(
        "ix_insider_trades_availability", "insider_trades", ["availability_time", "security_id"]
    )

    op.create_table(
        "insider_cluster_signals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("raised", sa.Boolean(), nullable=True),
        sa.Column("distinct_purchasers", sa.Integer(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("min_insiders", sa.Integer(), nullable=False),
        sa.Column("unavailable_reason", sa.Text(), nullable=True),
        sa.Column("config_version_label", sa.Text(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_insider_cluster_signals_security_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "security_id", "signal_date", name="uq_insider_cluster_signal_security_date"
        ),
        comment="One insider-buy-cluster reading per security per day. raised is tri-state.",
    )
    op.create_index(
        "ix_insider_cluster_signals_security",
        "insider_cluster_signals",
        ["security_id", "signal_date"],
    )

    # -- institutional_ownership / institutional_ownership_signals -----------
    op.create_table(
        "institutional_ownership",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("quarter", sa.Integer(), nullable=False),
        *_pit_columns(),
        sa.Column(
            "lineage", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_institutional_ownership_security_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "security_id", "year", "quarter", name="uq_institutional_ownership_security_period"
        ),
        comment="Raw 13F institutional-ownership summaries ingested from FMP. Never scored.",
    )
    op.create_index(
        "ix_institutional_ownership_security_period",
        "institutional_ownership",
        ["security_id", "year", "quarter"],
    )

    op.create_table(
        "institutional_ownership_signals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("quarter", sa.Integer(), nullable=False),
        sa.Column("investors_holding", sa.Integer(), nullable=True),
        sa.Column("investors_holding_change", sa.Integer(), nullable=True),
        sa.Column("total_shares", sa.Numeric(), nullable=True),
        sa.Column("total_shares_change_percent", sa.Numeric(), nullable=True),
        sa.Column("ownership_percent", sa.Numeric(), nullable=True),
        sa.Column("prior_year", sa.Integer(), nullable=True),
        sa.Column("prior_quarter", sa.Integer(), nullable=True),
        sa.Column("unavailable_reason", sa.Text(), nullable=True),
        sa.Column("config_version_label", sa.Text(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_institutional_ownership_signals_security_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "security_id", "year", "quarter", name="uq_institutional_ownership_signal_period"
        ),
        comment=(
            "One institutional-ownership trend reading per security per quarter. "
            "No raised column — a trend, not an event; the trader decides."
        ),
    )
    op.create_index(
        "ix_institutional_ownership_signals_security",
        "institutional_ownership_signals",
        ["security_id", "year", "quarter"],
    )


def downgrade() -> None:
    op.drop_table("institutional_ownership_signals")
    op.drop_index(
        "ix_institutional_ownership_security_period", table_name="institutional_ownership"
    )
    op.drop_table("institutional_ownership")
    op.drop_index("ix_insider_cluster_signals_security", table_name="insider_cluster_signals")
    op.drop_table("insider_cluster_signals")
    op.drop_index("ix_insider_trades_availability", table_name="insider_trades")
    op.drop_index("ix_insider_trades_security_time", table_name="insider_trades")
    op.drop_table("insider_trades")
    op.drop_index("ix_sec_filing_signals_security", table_name="sec_filing_signals")
    op.drop_table("sec_filing_signals")
    op.drop_index("ix_sec_filings_availability", table_name="sec_filings")
    op.drop_index("ix_sec_filings_security_time", table_name="sec_filings")
    op.drop_index("uq_sec_filings_no_link", table_name="sec_filings")
    op.drop_index("uq_sec_filings_link", table_name="sec_filings")
    op.drop_table("sec_filings")
