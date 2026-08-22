"""add UNCLASSIFIED market state and ticker-history exclusion constraints

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-22

Two corrections to the Module 03 baseline:

1. A ninth market state, UNCLASSIFIED. A newly listed security, or one
   with too little history to classify, must not be forced into
   DOWN_TREND — that would silently corrupt both the derived watchlists
   and any statistic computed over the distribution of states.

2. Exclusion constraints preventing overlapping validity ranges in
   security_ticker_history, in both directions: one security holds one
   ticker at a time, and one ticker maps to one security at a time
   (tickers get recycled after a delisting). Overlaps here make identity
   resolution produce wrong joins with nothing visibly failing, which is
   precisely the case where a database constraint earns its keep.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Columns typed with market_state_enum, needed to rebuild the type on downgrade.
MARKET_STATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("market_state", "state"),
    ("market_state_transitions", "from_state"),
    ("market_state_transitions", "to_state"),
    ("setup_outcomes", "market_regime_at_outcome"),
)

ORIGINAL_MARKET_STATES: tuple[str, ...] = (
    "DOWN_TREND",
    "BASE_FORMING",
    "CONSOLIDATION",
    "ACCUMULATION",
    "BREAKOUT_WATCH",
    "BREAKOUT_READY",
    "UPTREND",
    "DISTRIBUTION",
)


def upgrade() -> None:
    # Added BEFORE the cycle's first state so the database's enum sort
    # order matches the Python enum's declaration order.
    op.execute(
        "ALTER TYPE market_state_enum ADD VALUE IF NOT EXISTS 'UNCLASSIFIED' BEFORE 'DOWN_TREND'"
    )

    # Supplies gist operator classes for scalar equality, so security_id
    # and ticker can participate in a gist exclusion constraint alongside
    # a range. Standard contrib extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.execute(
        """
        ALTER TABLE security_ticker_history
        ADD CONSTRAINT excl_ticker_history_security_overlap
        EXCLUDE USING gist (
            security_id WITH =,
            tstzrange(valid_from, valid_to) WITH &&
        )
        """
    )
    op.execute(
        """
        ALTER TABLE security_ticker_history
        ADD CONSTRAINT excl_ticker_history_ticker_overlap
        EXCLUDE USING gist (
            ticker WITH =,
            tstzrange(valid_from, valid_to) WITH &&
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE security_ticker_history "
        "DROP CONSTRAINT IF EXISTS excl_ticker_history_ticker_overlap"
    )
    op.execute(
        "ALTER TABLE security_ticker_history "
        "DROP CONSTRAINT IF EXISTS excl_ticker_history_security_overlap"
    )
    op.execute("DROP EXTENSION IF EXISTS btree_gist")

    # PostgreSQL has no ALTER TYPE ... DROP VALUE, so the type is rebuilt
    # without UNCLASSIFIED and every dependent column re-pointed at it.
    # This deliberately fails if any row actually holds UNCLASSIFIED —
    # dropping the state would otherwise have to invent a value for those
    # rows, and silently relabelling a security's state is exactly the
    # corruption this state was added to prevent.
    labels = ", ".join(f"'{state}'" for state in ORIGINAL_MARKET_STATES)
    op.execute("ALTER TYPE market_state_enum RENAME TO market_state_enum_old")
    op.execute(f"CREATE TYPE market_state_enum AS ENUM ({labels})")
    for table, column in MARKET_STATE_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE market_state_enum USING {column}::text::market_state_enum"
        )
    op.execute("DROP TYPE market_state_enum_old")
