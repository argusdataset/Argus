"""record every login attempt, and give TOTP somewhere to remember itself

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-27

Module 22 turns the identity stub into real authentication. Module 03
left `users.password_hash` and `users.mfa_secret` deliberately unwritten
for this moment, and `sessions` was already shaped correctly — token
hashes only, expiry, revocation. Three things were missing.

## `login_attempts`, and why it is append-only

Brute-force protection needs to know how many times a credential has
just failed. That count has to live somewhere durable, and the somewhere
must not be erasable by the party the protection exists to stop: a
lockout you can clear with a DELETE is not a lockout.

So the table joins `APPEND_ONLY_TABLES`. That shapes the query too — a
lockout cannot be implemented as "reset the counter on success", because
there is no counter to reset. It is instead "failures recorded since the
last success, inside the window", which is the same rule expressed
against an immutable log and is the reason the log can be immutable.

Successes are recorded as well as failures. A table holding only
failures answers "was this attacked" and cannot answer "was this
attacked *successfully*", which is the question an incident starts with.

## `users.mfa_last_counter`

TOTP without replay protection is a one-time password that works twice.
A code stays valid for its whole time step, and an attacker who observes
one — over the shoulder, in a phishing proxy, in a log that should not
have had it — can use it inside that window unless the server remembers
that the step is spent. This column is that memory: the highest counter
this user has successfully consumed. Verification refuses anything at or
below it.

Nullable because a user who has never verified a code has consumed
nothing, which is different from having consumed step zero.

## `users.password_changed_at`

Written whenever the hash changes. Module 22 revokes every session on a
password change outright, so nothing reads this column yet — it is
recorded because "when did this credential last change" is the first
question asked about a compromised account, and reconstructing it from
`audit_log` works only for as long as nobody trims that table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from infra.db.append_only import create_guard_sql, drop_guard_sql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ATTEMPTS = "login_attempts"

#: The three roles ARGUS recognises. Seeded here rather than by the
#: service, because a deployment whose `roles` table is empty cannot
#: register anybody — the set is structural, not configuration.
ROLES: tuple[tuple[str, str], ...] = (
    (
        "public",
        "An unauthenticated caller. Never assigned to a users row; it names the "
        "privileges of a request that carries no session.",
    ),
    ("registered_user", "A signed-in person. Owns their own watchlists and nothing else."),
    ("admin", "Operational access, including the review gates. TOTP is required to use it."),
)


def upgrade() -> None:
    op.create_table(
        ATTEMPTS,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Lowercased at write time by the service. Stored even when it
        # matches no user: an attacker guessing addresses is exactly the
        # pattern this table exists to make visible.
        sa.Column("email", sa.Text(), nullable=False),
        # Null when the attempt names no existing account. The email
        # column still identifies what was being guessed.
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        # Why it failed, for the operator reading this later. Never the
        # credential that was tried.
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_login_attempts_user_id"),
            ondelete="SET NULL",
        ),
        comment="Append-only record of every authentication attempt (Module 22).",
    )
    # The lockout query's shape: attempts for one email, newest first.
    op.create_index("ix_login_attempts_email", ATTEMPTS, ["email", "attempted_at"])
    # The same question asked of one source address, which is how a
    # spray across many accounts becomes visible.
    op.create_index("ix_login_attempts_address", ATTEMPTS, ["ip_address", "attempted_at"])

    for statement in create_guard_sql(ATTEMPTS, allow_update=False):
        op.execute(statement)

    op.add_column(
        "users",
        sa.Column("mfa_last_counter", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Idempotent: a deployment that seeded these by hand keeps its rows
    # and their ids, so no user's role_id is invalidated by this running.
    for name, description in ROLES:
        op.execute(
            sa.text(
                "INSERT INTO roles (name, description) VALUES (:name, :description) "
                "ON CONFLICT (name) DO NOTHING"
            ).bindparams(name=name, description=description)
        )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM roles WHERE name IN :names").bindparams(
            sa.bindparam("names", value=tuple(name for name, _ in ROLES), expanding=True)
        )
    )
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "mfa_last_counter")
    for statement in drop_guard_sql(ATTEMPTS, allow_update=False):
        op.execute(statement)
    op.drop_index("ix_login_attempts_address", table_name=ATTEMPTS)
    op.drop_index("ix_login_attempts_email", table_name=ATTEMPTS)
    op.drop_table(ATTEMPTS)
