"""add listing intervals to universe_membership

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-22

Corrects a gap in the Module 03 baseline, found while building Module 06.

`universe_membership` recorded `listing_status` — a current-status flag —
but no date range. That makes the central question Module 07 has to
answer, "which securities were in the universe on date X", unanswerable:
a flag says what a security is now, not when it was listed, and the
information to reconstruct that is simply not present. It could not be
retrofitted later either, because the source data (FMP's delisted feed)
is only available at construction time.

Membership is therefore modelled as a dated join:

- `listed_from` / `listed_to` — the listing interval justifying inclusion,
  half-open, with NULL `listed_to` meaning still listed as of the
  version's as_of_date.
- `exchange` — the venue, per Module 05's normalize_exchange.
- `interval_evidence` — how the boundaries were established. FMP supplies
  no listing date for currently-listed securities, so many intervals are
  inferred from price history; recording the basis keeps an inferred
  boundary distinguishable from a reported one rather than letting the
  two look equally authoritative.

`listing_status` is kept: it still usefully describes the security as of
the version's as_of_date.

The table carries append-only triggers, but ALTER TABLE is DDL rather
than a row mutation, so they do not block this. NOT NULL is applied after
a backfill so the migration is safe against a non-empty table, even
though nothing has constructed a universe yet.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "universe_membership",
        sa.Column("listed_from", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "universe_membership",
        sa.Column("listed_to", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("universe_membership", sa.Column("exchange", sa.Text(), nullable=True))
    op.add_column(
        "universe_membership",
        sa.Column("interval_evidence", sa.Text(), nullable=False, server_default="unknown"),
    )

    # Backfill for any pre-existing rows. `created_at` is the only date the
    # old shape carried, and it is honest here: it is when the membership
    # was recorded, and the evidence marker says exactly that.
    op.execute(
        "UPDATE universe_membership "
        "SET listed_from = created_at, "
        "    exchange = 'UNKNOWN', "
        "    interval_evidence = 'backfilled_from_created_at' "
        "WHERE listed_from IS NULL"
    )

    op.alter_column("universe_membership", "listed_from", nullable=False)
    op.alter_column("universe_membership", "exchange", nullable=False)

    op.create_check_constraint(
        "listing_interval_ordered",
        "universe_membership",
        "listed_to IS NULL OR listed_to > listed_from",
    )
    op.create_index(
        "ix_universe_membership_interval",
        "universe_membership",
        ["universe_version_id", "listed_from", "listed_to"],
    )
    op.create_table_comment(
        "universe_membership",
        "Which securities were in which universe version, with the listing "
        "interval that justifies it.",
        existing_comment=(
            "Which securities were in which universe version, including delisted/bankrupt ones."
        ),
    )


def downgrade() -> None:
    op.create_table_comment(
        "universe_membership",
        "Which securities were in which universe version, including delisted/bankrupt ones.",
    )
    op.drop_index("ix_universe_membership_interval", table_name="universe_membership")
    # Bare name: the metadata naming convention expands it to
    # ck_universe_membership_listing_interval_ordered, exactly as it did on
    # create. Passing the expanded name here would get wrapped a second time.
    op.drop_constraint(
        "listing_interval_ordered",
        "universe_membership",
        type_="check",
    )
    op.drop_column("universe_membership", "interval_evidence")
    op.drop_column("universe_membership", "exchange")
    op.drop_column("universe_membership", "listed_to")
    op.drop_column("universe_membership", "listed_from")
