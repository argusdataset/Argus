"""Writing to `audit_log`. One function, a closed set of actions.

Module 03 built this table append-only and said why: "an audit trail that
can be edited is not an audit trail." Module 22 is the first module that
produces the sensitive actions it exists to hold.

## The payload rule, which is the whole risk here

An audit record of an authentication event is a record written at the
exact moment a credential is in memory. The temptation to include "what
was tried" is strongest here and would be the worst place in ARGUS to
give in to: `audit_log` is append-only, so a password written into it
cannot be deleted afterwards. Not redacted — *cannot be deleted*, by the
guard this project installed on purpose.

So `record` scrubs. It rejects a payload containing any key that names a
credential, loudly, rather than filtering silently — a silent filter
teaches callers that passing secrets is fine because something downstream
handles it, and the next caller passes one under a name the filter does
not know.

## What is recorded, and what an absence means

Actions are a closed tuple. A new sensitive action means adding a name
here, which is a visible change in a review rather than a new string
appearing in a call site.

Failed events are recorded as well as successful ones. `LOGIN_FAILED`
matters more than `LOGIN` for anyone reading this table after an
incident, and a trail that only holds successes describes a system where
nothing ever went wrong.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.engine import Connection

from infra.db.schema.users import audit_log

__all__ = [
    "ACTIONS",
    "LOGIN",
    "LOGIN_FAILED",
    "LOGOUT",
    "MFA_DISABLED",
    "MFA_ENROLLED",
    "MFA_VERIFIED",
    "PASSWORD_CHANGED",
    "REGISTERED",
    "ROLE_CHANGED",
    "SESSIONS_REVOKED",
    "CredentialInPayload",
    "record",
]

REGISTERED = "user.registered"
LOGIN = "user.login"
LOGIN_FAILED = "user.login_failed"
LOGOUT = "user.logout"
PASSWORD_CHANGED = "user.password_changed"
MFA_ENROLLED = "user.mfa_enrolled"
MFA_VERIFIED = "user.mfa_verified"
MFA_DISABLED = "user.mfa_disabled"
ROLE_CHANGED = "user.role_changed"
SESSIONS_REVOKED = "user.sessions_revoked"

#: Closed on purpose. Adding a sensitive action is a change here.
ACTIONS: tuple[str, ...] = (
    REGISTERED,
    LOGIN,
    LOGIN_FAILED,
    LOGOUT,
    PASSWORD_CHANGED,
    MFA_ENROLLED,
    MFA_VERIFIED,
    MFA_DISABLED,
    ROLE_CHANGED,
    SESSIONS_REVOKED,
)

#: Key names that must never reach an append-only table. Matched as
#: substrings against lowercased keys, so `new_password` and
#: `totp_secret_b32` are both caught.
_FORBIDDEN_KEY_PARTS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "otp",
        "mfa_code",
        "authorization",
        "cookie",
        "api_key",
        "apikey",
        "private",
    }
)


class CredentialInPayload(ValueError):
    """A payload named something that looks like a secret.

    Raised rather than filtered. A filter would let the call site keep
    passing secrets in the belief that something downstream copes, and
    the next key it does not recognise would go straight into a table
    nothing can delete from.
    """


class UnknownAuditAction(ValueError):
    """An action outside the closed set."""


def record(
    connection: Connection,
    action: str,
    *,
    actor_user_id: UUID | None = None,
    entity_type: str | None = "user",
    entity_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> UUID:
    """Append one audit record. Returns its id.

    `actor_user_id` is nullable in the schema for system-initiated
    actions, and is genuinely null for a failed login against an address
    that names no account — there is no actor, only an attempt, and the
    attempt is in `login_attempts` with the address that was tried.
    """
    if action not in ACTIONS:
        raise UnknownAuditAction(
            f"{action!r} is not one of the recorded actions. Add it to ACTIONS in "
            "services/identity/audit.py so the set stays readable in one place."
        )

    body = dict(payload or {})
    _refuse_credentials(body)

    return connection.execute(
        audit_log.insert()
        .values(
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=body,
            ip_address=ip_address,
        )
        .returning(audit_log.c.id)
    ).scalar_one()


def _refuse_credentials(payload: dict[str, Any], *, path: str = "") -> None:
    """Walk the payload and refuse anything named like a secret."""
    for key, value in payload.items():
        lowered = str(key).lower()
        where = f"{path}{key}"
        for part in _FORBIDDEN_KEY_PARTS:
            if part in lowered:
                raise CredentialInPayload(
                    f"Audit payload key {where!r} looks like a credential. "
                    "audit_log is append-only: a secret written here cannot be "
                    "deleted afterwards, only discovered. Record that the action "
                    "happened, never what was supplied."
                )
        if isinstance(value, dict):
            _refuse_credentials(value, path=f"{where}.")
