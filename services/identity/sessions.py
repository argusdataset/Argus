"""Creating, verifying and revoking sessions. What `current_user_id` calls.

## Verification asks three questions and answers one

Given a token: does a session row match its hash, is it unexpired, is it
unrevoked. All three failures return the same thing — no user — because
a caller able to tell "expired" from "revoked" from "never existed" can
probe which tokens once existed.

The expiry comparison is made in the database against the stored
`expires_at` rather than in Python against a value read out first. That
is not an optimisation: reading the row and then deciding leaves a window
where the row was valid at read time and expired by decision time, and
more importantly it means the truth about expiry lives in one place
rather than being re-derived by every caller.

## Revocation is a timestamp, not a delete

`sessions.revoked_at` was in Module 03's schema and this uses it rather
than deleting the row. A deleted session is indistinguishable from one
that never existed, and "when did this session end, and was it ended by a
logout or by expiry" is a question incident response asks. The row is
small and the history is worth more than the bytes.

## Everything about a user's sessions can be ended at once

`revoke_all_for_user` exists because a password change must invalidate
every session — otherwise changing a password after a compromise leaves
the attacker's session live, which is the exact opposite of what the
person doing it believes they achieved. It is called from `accounts.py`
on every password change, unconditionally, including the user's own
current session: making somebody sign in again after they change their
password is a trivial cost, and deciding which sessions are "probably
still fine" is a decision nobody has the information to make.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, desc, select
from sqlalchemy.engine import Connection

from infra.db.schema.users import sessions, users
from services.identity.config import IdentitySettings
from services.identity.tokens import hash_token, issue

__all__ = [
    "IssuedSession",
    "SessionRecord",
    "create",
    "list_for_user",
    "revoke",
    "revoke_all_for_user",
    "verify",
]


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A new session. `token` is returned once and is never recoverable."""

    session_id: UUID
    user_id: UUID
    token: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """A session as stored. Deliberately carries no token or hash."""

    session_id: UUID
    user_id: UUID
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    ip_address: str | None
    user_agent: str | None

    def active(self, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.revoked_at is None and self.expires_at > now


def create(
    connection: Connection,
    user_id: UUID,
    *,
    settings: IdentitySettings | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> IssuedSession:
    """Issue a session for a user who has already proven who they are.

    This function does not authenticate. It is called by `accounts.py`
    after the password and any second factor have been verified, and
    calling it anywhere else would be issuing a credential to somebody
    who has proven nothing — so nothing else calls it, and a structural
    test says so.
    """
    settings = settings or IdentitySettings()
    now = now or datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.session_lifetime_seconds)

    token, token_hash = issue(settings=settings)
    session_id = connection.execute(
        sessions.insert()
        .values(
            user_id=user_id,
            token_hash=token_hash,
            issued_at=now,
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        .returning(sessions.c.id)
    ).scalar_one()

    return IssuedSession(
        session_id=session_id,
        user_id=user_id,
        token=token,
        issued_at=now,
        expires_at=expires_at,
    )


def verify(connection: Connection, token: str, *, now: datetime | None = None) -> UUID | None:
    """The user this token names, or None. One answer for every failure.

    Also refuses a session belonging to a deactivated account. Without
    that, disabling a user would leave their live sessions working until
    each expired — a deactivation that does not take effect until tomorrow
    is not a deactivation.
    """
    now = now or datetime.now(UTC)
    return connection.execute(
        select(sessions.c.user_id)
        .select_from(sessions.join(users, users.c.id == sessions.c.user_id))
        .where(
            and_(
                sessions.c.token_hash == hash_token(token),
                sessions.c.revoked_at.is_(None),
                sessions.c.expires_at > now,
                users.c.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


def revoke(connection: Connection, token: str, *, now: datetime | None = None) -> UUID | None:
    """End one session by its token. Returns the user, or None if there was nothing.

    Idempotent: revoking an already-revoked or expired session is not an
    error. A logout that fails because you were already logged out would
    be a confusing thing to surface to somebody pressing a button.
    """
    now = now or datetime.now(UTC)
    row = connection.execute(
        sessions.update()
        .where(
            and_(
                sessions.c.token_hash == hash_token(token),
                sessions.c.revoked_at.is_(None),
            )
        )
        .values(revoked_at=now)
        .returning(sessions.c.user_id)
    ).scalar_one_or_none()
    return row


def revoke_all_for_user(
    connection: Connection, user_id: UUID, *, now: datetime | None = None
) -> int:
    """End every live session for a user. Returns how many were ended.

    Called on every password change, including the session making the
    change. See the module docstring on why "keep the current one" is a
    decision nobody has the information to make.
    """
    now = now or datetime.now(UTC)
    result = connection.execute(
        sessions.update()
        .where(and_(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None)))
        .values(revoked_at=now)
        .returning(sessions.c.id)
    ).all()
    return len(result)


def list_for_user(
    connection: Connection,
    user_id: UUID,
    *,
    limit: int = 50,
    include_ended: bool = False,
) -> list[SessionRecord]:
    """A user's own sessions, newest first. Never any token or hash."""
    query = select(sessions).where(sessions.c.user_id == user_id)
    if not include_ended:
        query = query.where(
            and_(sessions.c.revoked_at.is_(None), sessions.c.expires_at > datetime.now(UTC))
        )
    rows = connection.execute(query.order_by(desc(sessions.c.issued_at)).limit(limit)).all()
    return [
        SessionRecord(
            session_id=row.id,
            user_id=row.user_id,
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            ip_address=row.ip_address,
            user_agent=row.user_agent,
        )
        for row in rows
    ]
