"""User domain: accounts, roles, sessions, watchlists, audit, billing structure.

Structure only — Module 22 builds authentication logic, and no billing
logic is wired at all (ARGUS is not selling anything yet, by design; the
plan is to accumulate an honest multi-year track record first).

**On watchlists:** `user_watchlists` here are user-created, named and
manually edited. They are NOT the four ARGUS Intelligence watchlists
(DOWN TREND / CONSOLIDATION / BREAKOUT READY / UPTREND) — those are
derived views over `market_state` and are deliberately never stored as
their own table, because storing them would create a second source of
truth that could drift from the state engine. The two concepts must never
be merged.

`audit_log` is append-only: an audit trail that can be edited is not an
audit trail.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from infra.db.metadata import metadata

roles = Table(
    "roles",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    # RBAC at MVP is public / registered_user / admin.
    Column("name", Text, nullable=False, unique=True),
    Column("description", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment="RBAC roles. Module 22 wires the permission logic.",
)

users = Table(
    "users",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("email", Text, nullable=False, unique=True),
    Column("display_name", Text, nullable=True),
    # A single role per user is sufficient for the three-role MVP model;
    # a join table can be introduced later if roles become additive.
    Column(
        "role_id", UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    ),
    # Credential storage exists structurally so Module 22 has somewhere
    # to write; nothing here hashes, verifies, or issues anything.
    Column("password_hash", Text, nullable=True),
    Column("mfa_secret", Text, nullable=True),
    Column("mfa_enabled", Boolean, nullable=False, server_default="false"),
    # The highest TOTP counter this user has successfully consumed
    # (migration 0012). Without it a code works for its whole time step
    # rather than once, which is the difference between a one-time
    # password and a thirty-second one.
    Column("mfa_last_counter", BigInteger, nullable=True),
    # When the hash last changed (migration 0012). Nothing reads it —
    # Module 22 revokes every session on a password change outright —
    # but it is the first question asked about a compromised account.
    Column("password_changed_at", DateTime(timezone=True), nullable=True),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_users_role", "role_id"),
    comment="User accounts. Structure only — authentication logic is Module 22.",
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    ),
    # Hash, never the token itself — a leaked session table should not be
    # a leaked set of live credentials.
    Column("token_hash", Text, nullable=False, unique=True),
    Column("issued_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("ip_address", Text, nullable=True),
    Column("user_agent", Text, nullable=True),
    CheckConstraint("expires_at > issued_at", name="session_expiry_after_issue"),
    Index("ix_sessions_user", "user_id", "expires_at"),
    comment="Authentication sessions. Token hashes only.",
)

user_watchlists = Table(
    "user_watchlists",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    ),
    Column("name", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("user_id", "name", name="uq_user_watchlist_name"),
    comment="User-created watchlists. Distinct from ARGUS Intelligence derived watchlists.",
)

user_watchlist_items = Table(
    "user_watchlist_items",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "watchlist_id",
        UUID(as_uuid=True),
        ForeignKey("user_watchlists.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # References identity, not ticker — a watchlist entry survives a
    # ticker change like everything else in ARGUS.
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("position", Integer, nullable=True),
    Column("added_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("watchlist_id", "security_id", name="uq_watchlist_item"),
    Index("ix_watchlist_items_watchlist", "watchlist_id"),
    comment="Securities on a user watchlist.",
)

login_attempts = Table(
    "login_attempts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    # Lowercased at write time. Recorded even when it names no account:
    # an attacker guessing addresses is the pattern this table exists to
    # make visible, and dropping those rows would hide it.
    Column("email", Text, nullable=False),
    Column(
        "user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    ),
    Column("succeeded", Boolean, nullable=False),
    # Why it failed. Never what was tried.
    Column("reason", Text, nullable=True),
    Column("ip_address", Text, nullable=True),
    Column("user_agent", Text, nullable=True),
    Column("attempted_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_login_attempts_email", "email", "attempted_at"),
    Index("ix_login_attempts_address", "ip_address", "attempted_at"),
    comment="Append-only record of every authentication attempt (Module 22).",
)

registration_attempts = Table(
    "registration_attempts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("ip_address", Text, nullable=True),
    Column("succeeded", Boolean, nullable=False),
    # "email_taken", "weak_password", "rate_limited". Never the credential.
    Column("reason", Text, nullable=True),
    # Recorded for an operator's visibility, not part of the lockout key
    # — see migration 0013 on why this table counts by source address
    # alone.
    Column("email", Text, nullable=True),
    Column("attempted_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_registration_attempts_address", "ip_address", "attempted_at"),
    comment="Append-only record of every registration attempt, by source address (Module 24).",
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Nullable: system-initiated actions have no acting user.
    Column(
        "actor_user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("action", Text, nullable=False),
    Column("entity_type", Text, nullable=True),
    Column("entity_id", UUID(as_uuid=True), nullable=True),
    Column("payload", JSONB, nullable=False),
    Column("ip_address", Text, nullable=True),
    Index("ix_audit_log_occurred", "occurred_at"),
    Index("ix_audit_log_actor", "actor_user_id", "occurred_at"),
    Index("ix_audit_log_entity", "entity_type", "entity_id"),
    comment="Append-only audit trail.",
)

entitlements = Table(
    "entitlements",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("code", Text, nullable=False, unique=True),
    Column("description", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment="Entitlement definitions. Structure only — no logic wired.",
)

subscriptions = Table(
    "subscriptions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    ),
    Column(
        "entitlement_id",
        UUID(as_uuid=True),
        ForeignKey("entitlements.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("status", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ends_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_subscriptions_user", "user_id", "status"),
    comment="Subscription records. Structure only — no billing logic wired.",
)
