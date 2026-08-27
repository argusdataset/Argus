"""Registration, login, logout, password change. Where the pieces meet.

Every function here is a sequence of calls into `passwords`, `sessions`,
`attempts`, `mfa`, `roles` and `audit`. The ordering is the security
property, so it is worth stating why the login order is what it is:

    1. check the lockout        — before any expensive work, so a locked
                                  attacker gets a cheap refusal
    2. look the user up         — result never distinguishable in the answer
    3. verify the password      — always, even with no user (timing)
    4. check the account active — only after the password proved out
    5. verify the second factor — only after the password proved out
    6. issue the session
    7. record the attempt       — every path, success and failure alike

Steps 4 and 5 come after step 3 deliberately. "This account is disabled"
and "this account needs a code" are both statements about an account that
exists, and answering either before the password is verified would tell
an unauthenticated stranger which addresses are registered.

Step 7 has no early return above it. A failure path that forgets to
record its attempt is a hole in the lockout: whichever branch forgets
becomes the branch an attacker uses. `_fail` exists so that recording and
raising are one call and cannot drift apart.

## Nothing here logs a credential

Not the password, not the token, not the TOTP code, not the MFA secret.
`audit.record` refuses a payload key that looks like any of them, which
turns the convention into a mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from infra.db.schema.users import users
from services.identity import attempts, audit, mfa, sessions
from services.identity.config import IdentitySettings
from services.identity.errors import (
    ACCOUNT_INACTIVE,
    ACCOUNT_LOCKED,
    EMAIL_TAKEN,
    MFA_CODE_REQUIRED,
    WEAK_PASSWORD,
    IdentityError,
    invalid_credentials,
)
from services.identity.passwords import PasswordTooWeak, hash_password, verify_password
from services.identity.roles import REGISTERED_USER, role_id_for

__all__ = [
    "Account",
    "account_for",
    "change_password",
    "deactivate",
    "log_in",
    "log_out",
    "register",
]


@dataclass(frozen=True, slots=True)
class Account:
    """A user, as this service reports one. No hash, no secret, ever."""

    user_id: UUID
    email: str
    display_name: str | None
    role: str
    mfa_enabled: bool
    is_active: bool


def register(
    connection: Connection,
    *,
    email: str,
    password: str,
    display_name: str | None = None,
    role: str = REGISTERED_USER,
    settings: IdentitySettings | None = None,
    ip_address: str | None = None,
) -> Account:
    """Create an account. The password is hashed before it touches a row.

    Registration deliberately does *not* issue a session. Signing in is a
    separate act with its own lockout accounting and its own audit
    record, and folding it in here would create a second path to a
    session that skips both.
    """
    settings = settings or IdentitySettings()
    address = attempts.normalise_email(email)

    try:
        password_hash = hash_password(password, settings=settings)
    except PasswordTooWeak as weak:
        raise IdentityError(
            WEAK_PASSWORD,
            str(weak),
            status=400,
            detail={"minimum_length": weak.minimum},
        ) from weak

    if _user_by_email(connection, address) is not None:
        raise IdentityError(
            EMAIL_TAKEN,
            "An account already exists for this address.",
            status=409,
        )

    user_id = connection.execute(
        users.insert()
        .values(
            email=address,
            display_name=display_name,
            role_id=role_id_for(connection, role),
            password_hash=password_hash,
            password_changed_at=datetime.now(UTC),
        )
        .returning(users.c.id)
    ).scalar_one()

    audit.record(
        connection,
        audit.REGISTERED,
        actor_user_id=user_id,
        entity_id=user_id,
        payload={"email": address, "role": role},
        ip_address=ip_address,
    )
    return _account(connection, user_id)


def log_in(
    connection: Connection,
    *,
    email: str,
    password: str,
    mfa_code: str | None = None,
    settings: IdentitySettings | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> sessions.IssuedSession:
    """Verify a credential and issue a session. See the module docstring on order."""
    settings = settings or IdentitySettings()
    now = now or datetime.now(UTC)
    address = attempts.normalise_email(email)

    lockout = attempts.lockout_state(
        connection, email=address, ip_address=ip_address, settings=settings, now=now
    )
    if lockout.locked:
        # Recorded as an attempt so that hammering a locked account keeps
        # extending the record an operator reads, rather than going
        # invisible the moment the lock engages.
        raise _fail(
            connection,
            email=address,
            reason=attempts.LOCKED,
            ip_address=ip_address,
            user_agent=user_agent,
            at=now,
            error=IdentityError(
                ACCOUNT_LOCKED,
                "Too many failed sign-in attempts. Try again later.",
                status=429,
                detail={"retry_after_seconds": int(lockout.seconds_remaining(now=now))},
            ),
        )

    row = _user_by_email(connection, address)
    stored_hash = row.password_hash if row is not None else None

    # Always, including when there is no user — see `passwords.py` on why
    # a fast "no such account" is a remotely observable oracle.
    if not verify_password(password, stored_hash, settings=settings):
        raise _fail(
            connection,
            email=address,
            user_id=row.id if row is not None else None,
            reason=attempts.NO_SUCH_USER if row is None else attempts.BAD_PASSWORD,
            ip_address=ip_address,
            user_agent=user_agent,
            at=now,
            error=invalid_credentials(),
            audit_actor=row.id if row is not None else None,
        )

    assert row is not None  # verify_password returns False for a missing hash

    if not row.is_active:
        raise _fail(
            connection,
            email=address,
            user_id=row.id,
            reason=attempts.INACTIVE,
            ip_address=ip_address,
            user_agent=user_agent,
            at=now,
            error=IdentityError(
                ACCOUNT_INACTIVE,
                "This account is deactivated.",
                status=403,
            ),
            audit_actor=row.id,
        )

    if row.mfa_enabled and not (
        mfa_code and mfa.verify_code(connection, row.id, mfa_code, settings=settings, now=now)
    ):
        raise _fail(
            connection,
            email=address,
            user_id=row.id,
            reason=attempts.MFA_FAILED,
            ip_address=ip_address,
            user_agent=user_agent,
            at=now,
            error=IdentityError(
                MFA_CODE_REQUIRED,
                "This account requires a one-time code from its authenticator app.",
                status=401,
            ),
            audit_actor=row.id,
        )

    issued = sessions.create(
        connection,
        row.id,
        settings=settings,
        ip_address=ip_address,
        user_agent=user_agent,
        now=now,
    )
    attempts.record_attempt(
        connection,
        email=address,
        succeeded=True,
        user_id=row.id,
        ip_address=ip_address,
        user_agent=user_agent,
        at=now,
    )
    audit.record(
        connection,
        audit.LOGIN,
        actor_user_id=row.id,
        entity_id=row.id,
        payload={"session_id": str(issued.session_id), "mfa_used": bool(row.mfa_enabled)},
        ip_address=ip_address,
    )
    return issued


def log_out(
    connection: Connection,
    token: str,
    *,
    ip_address: str | None = None,
    now: datetime | None = None,
) -> bool:
    """End one session. True if something was ended.

    Idempotent, and silent about which case it was: a caller pressing
    "sign out" twice should see success twice.
    """
    user_id = sessions.revoke(connection, token, now=now)
    if user_id is None:
        return False
    audit.record(
        connection,
        audit.LOGOUT,
        actor_user_id=user_id,
        entity_id=user_id,
        payload={},
        ip_address=ip_address,
    )
    return True


def change_password(
    connection: Connection,
    user_id: UUID,
    *,
    current_password: str,
    new_password: str,
    settings: IdentitySettings | None = None,
    ip_address: str | None = None,
    now: datetime | None = None,
) -> int:
    """Replace the hash and end every session. Returns how many were ended.

    The current password is required even though the caller already holds
    a valid session. A session is evidence that somebody signed in as this
    person once; it is not evidence that the person at the keyboard right
    now knows the password. Without this check, an unattended laptop is a
    permanent account takeover.

    Every session is revoked, including the one making the request. See
    `sessions.revoke_all_for_user` on why keeping "probably fine" sessions
    is a decision nobody has the information to make.
    """
    settings = settings or IdentitySettings()
    now = now or datetime.now(UTC)

    row = connection.execute(select(users).where(users.c.id == user_id)).one_or_none()
    if row is None or not verify_password(current_password, row.password_hash, settings=settings):
        raise invalid_credentials()

    try:
        password_hash = hash_password(new_password, settings=settings)
    except PasswordTooWeak as weak:
        raise IdentityError(
            WEAK_PASSWORD, str(weak), status=400, detail={"minimum_length": weak.minimum}
        ) from weak

    connection.execute(
        users.update()
        .where(users.c.id == user_id)
        .values(password_hash=password_hash, password_changed_at=now, updated_at=now)
    )
    ended = sessions.revoke_all_for_user(connection, user_id, now=now)

    audit.record(
        connection,
        audit.PASSWORD_CHANGED,
        actor_user_id=user_id,
        entity_id=user_id,
        payload={"sessions_ended": ended},
        ip_address=ip_address,
    )
    return ended


def deactivate(
    connection: Connection,
    user_id: UUID,
    *,
    actor_user_id: UUID | None = None,
    ip_address: str | None = None,
    now: datetime | None = None,
) -> int:
    """Disable an account and end its sessions. Returns sessions ended.

    Both halves matter. Clearing `is_active` alone would leave live
    sessions working until each expired, and `sessions.verify` joins
    `users` precisely so that cannot happen — this revokes as well so the
    rows say what is true rather than relying on the join to hide them.
    """
    now = now or datetime.now(UTC)
    connection.execute(
        users.update().where(users.c.id == user_id).values(is_active=False, updated_at=now)
    )
    ended = sessions.revoke_all_for_user(connection, user_id, now=now)
    audit.record(
        connection,
        audit.SESSIONS_REVOKED,
        actor_user_id=actor_user_id or user_id,
        entity_id=user_id,
        payload={"sessions_ended": ended, "reason": "account_deactivated"},
        ip_address=ip_address,
    )
    return ended


def account_for(connection: Connection, user_id: UUID) -> Account:
    """One account, as this service reports it."""
    return _account(connection, user_id)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _user_by_email(connection: Connection, address: str):
    return connection.execute(select(users).where(users.c.email == address)).one_or_none()


def _account(connection: Connection, user_id: UUID) -> Account:
    from services.identity.roles import role_of

    row = connection.execute(select(users).where(users.c.id == user_id)).one()
    return Account(
        user_id=row.id,
        email=row.email,
        display_name=row.display_name,
        role=role_of(connection, user_id) or "",
        mfa_enabled=bool(row.mfa_enabled),
        is_active=bool(row.is_active),
    )


def _fail(
    connection: Connection,
    *,
    email: str,
    reason: str,
    error: IdentityError,
    user_id: UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
    audit_actor: UUID | None = None,
) -> IdentityError:
    """Record a failed attempt and hand back the error to raise.

    One call so that recording and refusing cannot drift apart. A branch
    that refused without recording would be a hole in the lockout, and it
    would be exactly the branch an attacker used.
    """
    attempts.record_attempt(
        connection,
        email=email,
        succeeded=False,
        user_id=user_id,
        reason=reason,
        ip_address=ip_address,
        user_agent=user_agent,
        at=at,
    )
    audit.record(
        connection,
        audit.LOGIN_FAILED,
        actor_user_id=audit_actor,
        entity_id=user_id,
        payload={"email": email, "reason": reason},
        ip_address=ip_address,
    )
    return error
