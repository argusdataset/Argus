"""append-only guards for four raw PIT tables that were missed

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-06

Four tables carry the full PIT column set — `event_time`,
`observation_time`, `availability_time`, `ingestion_time` — and are read
with `availability_time <= as_of`. That makes each one evidence of what
ARGUS knew at a given instant, which is exactly the property migration
0003's guard exists to protect, and none of them had it:

- `sec_filings`, `insider_trades`, `institutional_ownership` — created in
  0017 with no triggers at all.
- `pending_material_events` — older, and missed for longer.

`sec_filings`'s own schema docstring described it as "insert-only, like
`canonical_news`". `canonical_news` was guarded in 0010; this one was
not, and the difference was invisible because the drift test compared the
installed triggers against the declared list — so a table absent from
*both* satisfied it. That hole is closed in the same change, by a test
that starts from the schema rather than from the list.

An `UPDATE` on any of these would not merely lose data. It would rewrite
the record of what was knowable when, and every point-in-time claim
computed from that table afterwards would be uncheckable.

## No data is touched

Triggers only. The tables are empty in every environment today, and would
be safe to guard even if they were not: the guard refuses future
mutations and says nothing about existing rows.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported from `infra/db/append_only.py`. A
#: migration is a historical record of what was done on a date, and must
#: keep saying that after the module is edited again — the same reason
#: 0003 froze its own copy. `test_installed_guards_match_declared_tables`
#: is what keeps the two in step.
TABLES: tuple[str, ...] = (
    "sec_filings",
    "insider_trades",
    "institutional_ownership",
    "pending_material_events",
)


def upgrade() -> None:
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
