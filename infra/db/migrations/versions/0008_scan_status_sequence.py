"""give the review gate a monotonic order, so "latest status" is not a guess

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-23

Module 03's comment on `historical_scan_status` says "Current status is
the latest row per run", and until this migration the table had no way to
answer that reliably.

`assigned_at` defaults to `now()`, which in PostgreSQL is **transaction
start time**, not statement time. Three status assignments inside one
transaction therefore share a timestamp to the microsecond, and the `id`
tiebreak is `gen_random_uuid()` — random, not monotonic. So "the latest
row" was whichever UUID happened to sort highest.

In production the assignments come from separate transactions and the
timestamps differ, which is exactly what makes this the dangerous kind of
bug: it works until a reviewer approves and immediately corrects
themselves, or until a script batches decisions, and then the gate quietly
serves the wrong answer. This gate decides whether an unvalidated model's
numbers reach a public page; "usually right" is not the standard.

The fix is the pattern already in this schema for the same problem:
`setup_events.sequence_number`, monotonic per parent with a uniqueness
constraint, so event order is unambiguous even when two events share a
timestamp. `historical_scan_status` gets the same, per validation run.

Added NOT NULL in two steps, as in 0006 and 0007: nothing has ever written
this table, so the second step is a guard rather than a migration of real
rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEQUENCE_UNIQUE = "uq_scan_status_run_sequence"
SEQUENCE_CHECK = "sequence_non_negative"
LOOKUP_INDEX = "ix_scan_status_run"


def upgrade() -> None:
    op.add_column(
        "historical_scan_status",
        sa.Column("sequence_number", sa.Integer(), nullable=True),
    )
    op.alter_column("historical_scan_status", "sequence_number", nullable=False)
    op.create_unique_constraint(
        SEQUENCE_UNIQUE,
        "historical_scan_status",
        ["model_validation_run_id", "sequence_number"],
    )
    op.create_check_constraint(SEQUENCE_CHECK, "historical_scan_status", "sequence_number >= 0")

    # The lookup index led with `assigned_at`, which is the column that
    # turned out not to order anything. Rebuilt on the sequence so the
    # index serves the query `current_status` actually issues.
    op.drop_index(LOOKUP_INDEX, table_name="historical_scan_status")
    op.create_index(
        LOOKUP_INDEX, "historical_scan_status", ["model_validation_run_id", "sequence_number"]
    )


def downgrade() -> None:
    op.drop_index(LOOKUP_INDEX, table_name="historical_scan_status")
    op.create_index(
        LOOKUP_INDEX, "historical_scan_status", ["model_validation_run_id", "assigned_at"]
    )
    op.drop_constraint(
        op.f(f"ck_historical_scan_status_{SEQUENCE_CHECK}"),
        "historical_scan_status",
        type_="check",
    )
    op.drop_constraint(SEQUENCE_UNIQUE, "historical_scan_status", type_="unique")
    op.drop_column("historical_scan_status", "sequence_number")
