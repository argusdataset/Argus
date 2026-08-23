"""give outcomes a data snapshot, and make one-open-setup-per-security structural

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-23

Two corrections to the Module 03 baseline, both found while building
Module 14.

## `setup_outcomes` had no `data_snapshot_id`

Every other result table in ARGUS carries one, so a stored result can be
reproduced against exactly the data it was computed from. An outcome is
the most consequential result the system produces — the dataset every
later statistic is built on — and without the snapshot it would have been
the one result nobody could re-derive.

Added NOT NULL, in two steps so a non-empty table fails loudly rather than
acquiring nulls. Nothing writes this table yet, so in practice the second
step is a guard.

## `setups` permitted two open setups for one security

Module 14 maintains one-open-per-security in application code and flagged
that the schema did not enforce it. Migration 0005's partial-unique-index
pattern is the right shape, but it does not transfer unchanged, and the
difference matters:

`signals` could be constrained directly because `supersedes_signal_id` is
a real column on the row. A setup's openness is **derived from
`setup_events`**, and Module 03 deliberately gave `setups` no status
column — a partial index predicate cannot query another table, so there
was nothing to write a predicate against.

So this migration adds `concluded_at`: not a status column, a **terminal
marker**, NULL while the setup is open and set to the terminal event's
`occurred_at` when one is written. The partial unique index is then
`(security_id) WHERE concluded_at IS NULL`, exactly 0005's shape.

The precedent for a materialized projection alongside an append-only log
is already in this schema: Module 10's `market_state` is a projection of
`market_state_transitions`, and its own comment states the rule this
follows too — **the projection is rebuildable from the log, the log is not
rebuildable from the projection, and when they disagree the log is
right.** `core/lifecycle/derivation.py` continues to derive status from
events and never reads `concluded_at`; a test asserts the two agree for
every setup, so drift is visible rather than silent.

`setups` is guarded against DELETE but not UPDATE (Module 03's asymmetry,
for Module 15's review fields), so writing the marker is permitted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPEN_SETUP_INDEX = "uq_setups_open_per_security"


def upgrade() -> None:
    # -- Correction 1: outcomes cite the snapshot they were computed from
    op.add_column(
        "setup_outcomes",
        sa.Column("data_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_setup_outcomes_data_snapshot_id"),
        "setup_outcomes",
        "data_snapshot",
        ["data_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Fails loudly on a non-empty table rather than leaving unreproducible
    # rows behind. There is nothing to backfill an honest snapshot with.
    op.alter_column("setup_outcomes", "data_snapshot_id", nullable=False)
    op.create_index("ix_setup_outcomes_snapshot", "setup_outcomes", ["data_snapshot_id"])

    # -- Correction 2: at most one open setup per security
    op.add_column(
        "setups",
        sa.Column("concluded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        OPEN_SETUP_INDEX,
        "setups",
        ["security_id"],
        unique=True,
        postgresql_where=sa.text("concluded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(OPEN_SETUP_INDEX, table_name="setups")
    op.drop_column("setups", "concluded_at")
    op.drop_index("ix_setup_outcomes_snapshot", table_name="setup_outcomes")
    op.drop_constraint(
        op.f("fk_setup_outcomes_data_snapshot_id"), "setup_outcomes", type_="foreignkey"
    )
    op.drop_column("setup_outcomes", "data_snapshot_id")
