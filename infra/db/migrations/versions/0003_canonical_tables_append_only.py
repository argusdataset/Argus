"""guard the canonical data tables as append-only

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-22

Closes a gap in the Module 03 baseline. `infra/db/schema/canonical.py`
documents "restatements are new rows, never edits", and the uniqueness
constraints on all three canonical tables include `observation_time`
precisely so the same `event_time` can carry several observations. But
the append-only triggers were never installed on those tables, so the
doctrine was policy rather than structure and an UPDATE would have
silently rewritten history.

This matters more here than on most tables. A canonical row records what
ARGUS believed at a point in time. Editing one in place destroys the
evidence of that belief, and every historical claim computed from it
becomes uncheckable — which is the one thing ARGUS cannot afford, since
its entire value rests on those claims being verifiable.

Module 05's writer is insert-only by construction; this makes it
insert-only by enforcement, for every caller.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CANONICAL_TABLES: tuple[str, ...] = (
    "canonical_ohlcv",
    "canonical_fundamentals",
    "canonical_corporate_actions",
)


def upgrade() -> None:
    for table in CANONICAL_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION argus_reject_mutation();"
        )
        # TRUNCATE does not fire row-level triggers, so without this the
        # whole table would stay erasable in a single statement.
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION argus_reject_mutation();"
        )


def downgrade() -> None:
    for table in CANONICAL_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table};")
