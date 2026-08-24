"""gate live outcomes for publication, and materialize the public charts

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-24

Two tables, for two different jobs Module 20 needs.

## `public_release_windows` answers a question no prior module had

Module 17 gates historical results. Module 18 deliberately does not gate
live scans — a scan succeeding is the system working, not a result under
review. Nobody had said whether an *outcome* produced during live
scanning may be published.

Module 20's answer is yes, but only inside a window a named human
approved. The reasoning is in `services/public_stats/releases.py`. The
shape here is `historical_scan_status`'s, deliberately: append-only
status assignments, `sequence_number` monotonic per window, current
status being the highest sequence.

That includes migration 0008's lesson. `assigned_at` defaults to `now()`,
which is transaction start time, and the `id` tiebreak is a random UUID —
so two decisions in one transaction would order arbitrarily. An explicit
counter with database-enforced uniqueness is the mechanism that does not
have that failure mode, and this table has it from the start rather than
acquiring it after the bug.

## `public_stat_snapshots` makes the public page cheap without making it stale

The public page is the one surface expected to take real traffic with no
login. Recomputing four aggregates over every published outcome per
request is the wrong shape, so the payloads are materialized.

`gate_fingerprint` is what makes that safe. A snapshot records the exact
set of approved runs and windows it drew from. When a run is later
REJECTED the fingerprint stops matching and the snapshot is refused
rather than served — otherwise materialization would mean a withdrawn
result staying on the public page until somebody remembered to refresh,
which is the quiet inflation the gate exists to prevent.

Neither table is append-only-guarded. `public_release_windows` is
append-only by construction — nothing updates a row, every decision is a
new one — and adding a trigger would be belt-and-braces on a table whose
service never issues an UPDATE. `public_stat_snapshots` is a cache;
its rows are derived, disposable, and recomputable from the tables that
are guarded.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WINDOWS = "public_release_windows"
SNAPSHOTS = "public_stat_snapshots"


def upgrade() -> None:
    # The enum already exists from migration 0001 (Module 17's gate uses
    # it); naming it with create_type=False reuses it rather than
    # attempting a second CREATE TYPE. Reusing it is the point: a
    # reviewer moves live results through the same three states.
    status = postgresql.ENUM(
        "PENDING_REVIEW",
        "APPROVED",
        "REJECTED",
        name="historical_scan_status_enum",
        create_type=False,
    )

    op.create_table(
        WINDOWS,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("data_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("assigned_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["data_snapshot_id"],
            ["data_snapshot.id"],
            name=op.f("fk_public_release_windows_data_snapshot_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"],
            ["users.id"],
            name=op.f("fk_public_release_windows_assigned_by_user_id"),
        ),
        sa.UniqueConstraint(
            "period_start", "period_end", "sequence_number", name="uq_release_window_sequence"
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_public_release_windows_window_ordered")
        ),
        sa.CheckConstraint(
            "sequence_number >= 0", name=op.f("ck_public_release_windows_sequence_non_negative")
        ),
        comment="Append-only review gate for publishing live-tracked outcomes (Module 20).",
    )
    op.create_index(
        "ix_release_window_period", WINDOWS, ["period_start", "period_end", "sequence_number"]
    )

    op.create_table(
        SNAPSHOTS,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("chart", sa.Text(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gate_fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "included_runs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "included_windows",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "sample_size >= 0", name=op.f("ck_public_stat_snapshots_sample_size_non_negative")
        ),
        comment=(
            "Materialized public chart payloads, with the gate state they were computed under."
        ),
    )
    op.create_index("ix_stat_snapshot_chart", SNAPSHOTS, ["chart", "computed_at"])


def downgrade() -> None:
    op.drop_index("ix_stat_snapshot_chart", table_name=SNAPSHOTS)
    op.drop_table(SNAPSHOTS)
    op.drop_index("ix_release_window_period", table_name=WINDOWS)
    op.drop_table(WINDOWS)
