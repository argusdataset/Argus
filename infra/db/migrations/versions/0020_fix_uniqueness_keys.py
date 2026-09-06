"""three uniqueness keys that lose data or duplicate it forever

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-06

Three natural keys were wrong in two different ways, and both ways are
silent — no error, no log line, just a table that is quietly incomplete
or quietly unbounded.

## 1. `institutional_ownership` froze the first observation of a quarter

The key was `(security_id, year, quarter)`. 13F filings arrive across the
45 days after a quarter closes, and amendments (13F-HR/A) arrive later
still — all under the same quarter. Writes are `ON CONFLICT DO NOTHING`
and the ingestion re-requests the whole history each run, so the *first*
observation of a quarter was kept and every later, more complete one was
discarded.

The consequence is worse than a stale number. If the first fetch of Q4
saw 120 of the eventual 340 filers, that 120 is permanent; when Q1 fills
in properly the comparison reports roughly 215 institutions leaving —
a fabricated mass exodus, in the one signal the table exists to produce.

**Adding `observation_time` to the key, which is what every other
canonical table does, would not have fixed it.** That column is derived
here from the quarter end plus the 45-day deadline, so it is identical
for every fetch of a quarter however many times the figures change. The
key needs to distinguish "the same numbers again" from "different
numbers later", and only the numbers can do that — hence
`content_fingerprint`, a stable hash of the figures ARGUS reads,
resolved through the same `FIELD_ALIASES` everything else uses.

`observation_time` is corrected in the same change, in
`translate_institutional_ownership`: it is now the later of the deadline
and the fetch instant. The deadline stays a floor, because nothing is
public before it; the fetch instant is the rest, because a revision seen
in March was not knowable in February and saying otherwise is the same
leak from the other side.

## 2 and 3. Nullable columns in a key make `ON CONFLICT` a no-op

`analyst_grades.new_grade` is nullable, and so are three of the five
columns in `insider_trades`'s key. Under Postgres's default rule two
NULLs are never equal, so a row with a NULL in its key matches nothing —
`ON CONFLICT DO NOTHING` never fires and re-ingestion appends the same
row again on every run, into tables that are append-only and therefore
cannot be cleaned up afterwards.

This is not hypothetical for `analyst_grades`: FMP's field names for
that endpoint are documented but unverified, and
`translate_grade` stores `None` when none of the aliases resolve. One
wrong spelling would mean a security in BREAKOUT_READY accumulating its
entire grade history daily, forever.

`NULLS NOT DISTINCT` (PostgreSQL 15+) fixes it without touching the
data. The columns stay nullable, which is correct — `normalize_transaction_code`
returns None for a code it does not recognise precisely so an unknown
code is never counted as a purchase, and a NOT NULL sentinel would be a
value that lies about what was read. This only makes two
identically-unreadable rows the same row, which is what they are.

## This migration is refused by default, and that is correct

`infra/deploy/migrate.py` stops a deploy whose migration would break
containers still running the previous code. This one qualifies twice
over: it drops constraints that older code names in its `ON CONFLICT`
clause, and it adds a NOT NULL column older code does not supply. The
first deploy carrying it therefore fails at the migration step, and every
web service then fails its health check — `check_health` reports `down`
when the schema is not the revision the code expects.

The remedy is `ARGUS_ALLOW_DESTRUCTIVE_MIGRATION=1` for one deploy, and
it is safe here for a specific reason rather than as a general dispensation:
the only writer of these three tables is the ingestion cron, which is
replaced wholesale by the same deploy rather than rolling, so no old code
writes them once the new image exists. `ACKNOWLEDGED_DESTRUCTIVE` records
this, and `tests/unit/deploy/test_migration_safety.py` fails if a
destructive migration is ever added without such a note.

## Rebuilding a unique constraint is not rebuilding the data

All three tables are empty in every environment today. Even if they were
not, dropping and recreating a constraint touches no rows — and the
widened `institutional_ownership` key is strictly more permissive, so no
existing row could violate it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. institutional_ownership: key on the figures, not on the quarter.
    #
    # Added nullable and then made NOT NULL, with no backfill and no
    # server default. All three of those are deliberate:
    #
    # - A `DEFAULT md5(data::text)` is not expressible — Postgres refuses
    #   a column reference in a DEFAULT.
    # - An `UPDATE` backfill would be refused outright: migration 0019
    #   put an append-only trigger on this table one revision ago.
    # - A constant default would write the *same* fingerprint onto every
    #   pre-existing row, which is the collision this key exists to
    #   prevent, dressed up as a successful migration.
    #
    # So on a table with rows, the `NOT NULL` below fails and says so.
    # That is the right outcome: the table is empty in every environment
    # today, and if it ever is not, choosing what those rows' fingerprints
    # should be is a decision for a person rather than for a default.
    op.add_column(
        "institutional_ownership",
        sa.Column("content_fingerprint", sa.Text(), nullable=True),
    )
    op.alter_column(
        "institutional_ownership",
        "content_fingerprint",
        existing_type=sa.Text(),
        nullable=False,
    )

    op.drop_constraint(
        "uq_institutional_ownership_security_period",
        "institutional_ownership",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_institutional_ownership_reading",
        "institutional_ownership",
        ["security_id", "year", "quarter", "content_fingerprint"],
    )

    # 2 and 3. NULLS NOT DISTINCT. Alembic's `create_unique_constraint`
    # has no argument for it, so these are raw DDL — the alternative
    # would be an `op.execute` for the drop as well, which is the same
    # thing with more lines.
    op.drop_constraint("uq_analyst_grade_action", "analyst_grades", type_="unique")
    op.execute(
        """
        ALTER TABLE analyst_grades
        ADD CONSTRAINT uq_analyst_grade_action
        UNIQUE NULLS NOT DISTINCT (security_id, grading_company, event_time, new_grade)
        """
    )

    op.drop_constraint("uq_insider_trade_natural_key", "insider_trades", type_="unique")
    op.execute(
        """
        ALTER TABLE insider_trades
        ADD CONSTRAINT uq_insider_trade_natural_key
        UNIQUE NULLS NOT DISTINCT
        (security_id, reporting_person, event_time, transaction_code, quantity)
        """
    )


def downgrade() -> None:
    op.drop_constraint("uq_insider_trade_natural_key", "insider_trades", type_="unique")
    op.create_unique_constraint(
        "uq_insider_trade_natural_key",
        "insider_trades",
        ["security_id", "reporting_person", "event_time", "transaction_code", "quantity"],
    )

    op.drop_constraint("uq_analyst_grade_action", "analyst_grades", type_="unique")
    op.create_unique_constraint(
        "uq_analyst_grade_action",
        "analyst_grades",
        ["security_id", "grading_company", "event_time", "new_grade"],
    )

    op.drop_constraint(
        "uq_institutional_ownership_reading", "institutional_ownership", type_="unique"
    )
    op.create_unique_constraint(
        "uq_institutional_ownership_security_period",
        "institutional_ownership",
        ["security_id", "year", "quarter"],
    )
    op.drop_column("institutional_ownership", "content_fingerprint")
