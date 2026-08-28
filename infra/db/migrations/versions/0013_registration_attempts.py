"""record every registration attempt, for Module 24's per-IP signup limit

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-28

Module 22 built `login_attempts` to make a lockout possible against a
table that cannot be cleared. Registration needs the same shape for a
different attack: not credential guessing against one account, but bulk
account creation from one source.

The dimension being defended is different from `login_attempts`, which
counts failures against one **email** because the harm is guessing one
person's password. Registration has no victim account yet; the harm is
volume from one **source**, so the lockout here is keyed on `ip_address`
alone. `email` is still recorded — an operator asking "what was this
address trying to create" needs it — but it plays no part in the
counting query, which is why it is nullable and unindexed rather than
carrying the composite index `login_attempts.email` has.

Append-only, for the same reason as `login_attempts`: a rate limit
clearable by deleting its own evidence is not a rate limit.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from infra.db.append_only import create_guard_sql, drop_guard_sql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "registration_attempts"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        # Why it failed: "email_taken", "weak_password", "rate_limited".
        # Never the credential.
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment="Append-only record of every registration attempt, by source address (Module 24).",
    )
    op.create_index("ix_registration_attempts_address", TABLE, ["ip_address", "attempted_at"])

    for statement in create_guard_sql(TABLE, allow_update=False):
        op.execute(statement)


def downgrade() -> None:
    for statement in drop_guard_sql(TABLE, allow_update=False):
        op.execute(statement)
    op.drop_index("ix_registration_attempts_address", table_name=TABLE)
    op.drop_table(TABLE)
