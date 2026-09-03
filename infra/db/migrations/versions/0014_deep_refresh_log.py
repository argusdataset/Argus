"""record each completed tiered deep refresh, for Module 26's due-ness decision

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-03

Module 26 refreshes fundamentals and news at a frequency set by a
security's current watchlist phase. Deciding whether a security is due
needs one durable fact: when its last completed refresh happened, and
which phase drove it.

## A log, not a mutable per-security row

The alternative shape — one row per security, overwritten each time —
was rejected for a specific reason rather than a stylistic one.
`core/market_state/watchlists.py` states the Source-of-Truth principle
plainly: a watchlist is a filter over the `market_state` projection and
never an independently stored value, because a second stored copy of
"current state" is exactly the thing that quietly disagrees with the
real one.

A `current_phase` column here would be that second copy. What this table
stores instead is the phase that drove **one completed refresh** — a
historical fact about a past event, which stays true no matter what the
security does next. Module 26 reads current phase live at decision time
and uses this table only for "what happened last time".

## `refreshed_on` is a date

The tier intervals are declared in whole days and the job runs once per
trading day, so due-ness is decided in days. Comparing timestamps
instead would let a cron firing that drifts a minute early skip a
daily-tier security by arriving 23h 59m after the last refresh.
`refreshed_at` keeps the instant for anyone reading a run's history.

`(security_id, refreshed_on)` unique is also the durable half of "a
re-run of the same day is a no-op": Module 26 reuses Module 04's JSONL
checkpoint for in-run resumability, but a Railway cron container starts
with an empty filesystem, so the checkpoint cannot be what makes a
re-run idempotent across firings. This constraint can be.

## Not append-only guarded

Same reasoning as `live_scan_runs` (migration 0009). Module 03's guards
protect rows whose editing would rewrite what ARGUS believed at a point
in time; this is an operational record of a job, and no statistic is
computed from it. Note the consequence: it is therefore *not* eligible
for Module 25's `MONITORED_TABLES`, which asserts every table it watches
is guarded. Its growth is bounded by the number of due securities per
day — see `core/ingestion/README.md`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "deep_refresh_log"


def upgrade() -> None:
    # The enum already exists (migration 0001 created it for
    # `market_state`). Referenced, never re-created.
    state = postgresql.ENUM(name="market_state_enum", create_type=False)

    op.create_table(
        TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("refreshed_on", sa.Date(), nullable=False),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("triggering_watchlist", sa.Text(), nullable=False),
        sa.Column("market_state", state, nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("statements_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("news_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("config_version_label", sa.Text(), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_deep_refresh_log_security_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("security_id", "refreshed_on", name="uq_deep_refresh_security_date"),
        comment=(
            "One completed tiered deep refresh of one security (Module 26). "
            "triggering_watchlist is a historical fact about this refresh, never "
            "an authoritative 'current phase'."
        ),
    )
    op.create_index("ix_deep_refresh_latest", TABLE, ["security_id", "refreshed_on"])


def downgrade() -> None:
    op.drop_index("ix_deep_refresh_latest", table_name=TABLE)
    op.drop_table(TABLE)
