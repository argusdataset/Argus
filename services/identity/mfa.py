"""TOTP: enrolment, verification, and the counter that makes it one-time.

## The requirement-scope decision, stated rather than assumed

**TOTP is required for `admin` and optional for `registered_user`.**

The reasoning, in full, because the brief asks for a decision and not a
default:

*Why not required for everyone.* ARGUS has no account-recovery flow — no
password reset email, no backup codes, no support desk. Mandating a
second factor in that state means the first person to reset their phone
is permanently locked out of their own watchlist with no path back. That
is a certain availability harm traded against a marginal confidentiality
gain, because what a `registered_user` account holds is a personal list
of tickers. Requiring MFA before building recovery is putting the lock on
before the door.

*Why required for admin anyway.* An admin account is the one whose
compromise reaches other people's data and the review gates that decide
what ARGUS publishes about its own track record. The asymmetry is the
point: the blast radius is different, so the requirement is different.
There are few admins, they are operators rather than customers, and an
operator locked out is a fixable problem in a way a customer locked out
is not.

*How the requirement is enforced.* Not at login — an admin who cannot log
in cannot enrol, which would make the requirement unsatisfiable.
`require_role(connection, user_id, ADMIN)` refuses with `MFA_REQUIRED`
until enrolment is complete. So an admin without a second factor is an
ordinary signed-in user with an enrolment page, which is exactly what
they should be.

*What would change this.* Building account recovery. Once a locked-out
person has a way back, requiring TOTP for everyone costs little and this
decision should be revisited — it is recorded here so the revisit has
something to argue with.

## Replay: why `mfa_last_counter` exists

A TOTP code is valid for its whole time step, and with a skew window of
one step it is accepted across ninety seconds. Without a record of what
has been spent, a code observed once — over a shoulder, through a
phishing proxy, in a log that should not have had it — works again inside
that window. That is a one-time password that works twice.

So verification records the counter it accepted and refuses anything at
or below it. The cost is real and worth naming: a person who types a
correct code that races with a replay of the same code sees one of the
two rejected. That is the correct outcome, and it is the reason the
column is `mfa_last_counter` rather than a boolean.

## Enrolment is two steps, deliberately

`begin_enrolment` generates a secret and stores it with `mfa_enabled`
still false. `complete_enrolment` verifies a code the person generated
from it and only then flips the flag. One step would enable MFA on an
account whose owner had not yet proven their authenticator was working —
locking them out with a factor they cannot produce, at the exact moment
they were trying to make their account safer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pyotp
from sqlalchemy import select
from sqlalchemy.engine import Connection

from infra.db.schema.users import users
from services.identity.config import IdentitySettings
from services.identity.errors import (
    MFA_ALREADY_ENROLLED,
    MFA_NOT_ENROLLED,
    IdentityError,
)

__all__ = [
    "ISSUER",
    "Enrolment",
    "begin_enrolment",
    "complete_enrolment",
    "counter_for",
    "disable",
    "is_enabled",
    "verify_code",
]

#: What an authenticator app shows beside the account. Cosmetic, and
#: fixed so a person with several ARGUS accounts can tell them apart by
#: the email rather than by guessing.
ISSUER = "ARGUS"


@dataclass(frozen=True, slots=True)
class Enrolment:
    """A started enrolment. `secret` is shown once, at enrolment time."""

    secret: str
    provisioning_uri: str


def counter_for(moment: datetime, *, settings: IdentitySettings | None = None) -> int:
    """The TOTP counter (time step number) covering this instant."""
    settings = settings or IdentitySettings()
    return int(moment.timestamp()) // int(settings.totp_step_seconds)


def is_enabled(connection: Connection, user_id: UUID) -> bool:
    return bool(
        connection.execute(
            select(users.c.mfa_enabled).where(users.c.id == user_id)
        ).scalar_one_or_none()
    )


def begin_enrolment(
    connection: Connection,
    user_id: UUID,
    *,
    email: str,
    settings: IdentitySettings | None = None,
) -> Enrolment:
    """Generate and store a secret. Does not enable MFA — see the docstring.

    Refuses an account that already has MFA enabled: silently replacing a
    working second factor with a new one would be a full account takeover
    for anybody who reached this endpoint with a stolen session.
    Disabling first is a separate, audited action.
    """
    if is_enabled(connection, user_id):
        raise IdentityError(
            MFA_ALREADY_ENROLLED,
            "This account already has a second factor. Disable the existing one "
            "before enrolling another.",
            status=409,
        )

    secret = pyotp.random_base32()
    connection.execute(
        users.update()
        .where(users.c.id == user_id)
        .values(mfa_secret=secret, mfa_enabled=False, mfa_last_counter=None)
    )
    uri = pyotp.TOTP(
        secret, interval=int((settings or IdentitySettings()).totp_step_seconds)
    ).provisioning_uri(name=email, issuer_name=ISSUER)
    return Enrolment(secret=secret, provisioning_uri=uri)


def complete_enrolment(
    connection: Connection,
    user_id: UUID,
    code: str,
    *,
    settings: IdentitySettings | None = None,
    now: datetime | None = None,
) -> bool:
    """Verify a code against the pending secret and enable MFA if it matches.

    Only here does `mfa_enabled` become true, and only against a code the
    person produced from their own authenticator.
    """
    secret = _secret(connection, user_id)
    if secret is None:
        raise IdentityError(
            MFA_NOT_ENROLLED,
            "No enrolment is in progress for this account. Start one at /identity/mfa/enroll.",
            status=409,
        )

    if not _check(connection, user_id, secret, code, settings=settings, now=now):
        return False

    connection.execute(users.update().where(users.c.id == user_id).values(mfa_enabled=True))
    return True


def verify_code(
    connection: Connection,
    user_id: UUID,
    code: str,
    *,
    settings: IdentitySettings | None = None,
    now: datetime | None = None,
) -> bool:
    """Whether this code is valid and unspent. False if MFA is not enabled."""
    secret = _secret(connection, user_id)
    if secret is None or not is_enabled(connection, user_id):
        return False
    return _check(connection, user_id, secret, code, settings=settings, now=now)


def disable(connection: Connection, user_id: UUID) -> None:
    """Remove the second factor entirely.

    Clears the secret as well as the flag. Leaving a stale secret behind
    would mean a later re-enrolment silently reusing a value that may have
    been exposed, which is the reason it was being removed.
    """
    connection.execute(
        users.update()
        .where(users.c.id == user_id)
        .values(mfa_secret=None, mfa_enabled=False, mfa_last_counter=None)
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _secret(connection: Connection, user_id: UUID) -> str | None:
    return connection.execute(
        select(users.c.mfa_secret).where(users.c.id == user_id)
    ).scalar_one_or_none()


def _check(
    connection: Connection,
    user_id: UUID,
    secret: str,
    code: str,
    *,
    settings: IdentitySettings | None = None,
    now: datetime | None = None,
) -> bool:
    """Match the code against every counter in the skew window, then spend it.

    pyotp's own `verify(valid_window=...)` would answer the match question
    but not tell us *which* counter matched, and the counter is what has
    to be recorded to stop a replay. So the window is walked explicitly.
    """
    settings = settings or IdentitySettings()
    now = now or datetime.now(UTC)
    step = int(settings.totp_step_seconds)
    skew = int(settings.totp_skew_steps)

    totp = pyotp.TOTP(secret, interval=step)
    current = counter_for(now, settings=settings)
    last_used = connection.execute(
        select(users.c.mfa_last_counter).where(users.c.id == user_id)
    ).scalar_one_or_none()

    for offset in range(-skew, skew + 1):
        counter = current + offset
        if last_used is not None and counter <= int(last_used):
            # Already spent, or older than something spent. Refusing the
            # whole prefix rather than just the exact counter closes the
            # replay of an *earlier* code inside the same window.
            continue
        if totp.verify(code, for_time=datetime.fromtimestamp(counter * step, tz=UTC)):
            connection.execute(
                users.update().where(users.c.id == user_id).values(mfa_last_counter=counter)
            )
            return True
    return False
