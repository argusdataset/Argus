"""Brute-force protection, counted from an immutable log.

## The lockout rule, and why it is shaped the way it is

`login_attempts` is append-only (migration 0012), for a reason that is
almost the definition of the feature: a lockout you can clear with a
DELETE is not a lockout. That constraint decides the algorithm.

The obvious design — a `failed_count` column reset to zero on success —
requires an UPDATE and a DELETE-able counter, and is unavailable. The
rule here is instead:

    count the failures recorded **since this address last succeeded**,
    inside the lockout window; lock if that reaches the threshold.

Which is the same policy expressed against an immutable log, and is
strictly better than a counter in one way worth naming: the log still
holds the failures after the lock lifts, so an operator asking "was this
account attacked last Tuesday" has an answer. A reset counter has thrown
that away by design.

## Two limits, because one is famously evadable

**Per address** (the email being guessed): stops someone working through
a password list against one account.

**Per source** (the IP the attempts come from): stops the attack the
first limit is blind to — one password sprayed across ten thousand
accounts, which never trips a per-account threshold because no single
account sees more than one failure. The per-source limit is deliberately
looser (25 vs 5): a shared office NAT genuinely produces more failures
than one person does, and locking a whole building out of ARGUS to stop
a spray would be a denial of service performed on the defender's behalf.

## What is recorded

Every attempt, successful or not, with the email that was tried even when
it names no account — an attacker enumerating addresses is exactly the
pattern this table exists to make visible, and dropping the rows that
matched nothing would hide it.

Never what was tried. `reason` says `bad_password`, not the password.

## The address is normalised before it is counted

Lowercased and stripped, the same normalisation `accounts.py` applies
before looking a user up. Without that, `Alice@x.test` and
`alice@x.test` would be one account with two independent failure
budgets — a lockout evaded by pressing shift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, desc, func, select
from sqlalchemy.engine import Connection

from infra.db.schema.users import login_attempts
from services.identity.config import IdentitySettings

__all__ = [
    "BAD_PASSWORD",
    "INACTIVE",
    "LOCKED",
    "MFA_FAILED",
    "NO_SUCH_USER",
    "Lockout",
    "lockout_state",
    "normalise_email",
    "record_attempt",
]

#: Failure reasons. Descriptive for an operator; never the credential.
NO_SUCH_USER = "no_such_user"
BAD_PASSWORD = "bad_password"
MFA_FAILED = "mfa_failed"
INACTIVE = "account_inactive"
LOCKED = "locked_out"


def normalise_email(email: str) -> str:
    """Lowercased and stripped.

    Applied identically here and in `accounts.py`. If the two ever
    disagreed, a lockout would be evaded by changing the case of a
    letter — so a test asserts one function is used in both places.
    """
    return email.strip().lower()


@dataclass(frozen=True, slots=True)
class Lockout:
    """Whether an attempt may proceed, and until when if not."""

    locked: bool
    until: datetime | None = None
    #: Which limit tripped: "email" or "address". Recorded for the
    #: operator; the caller's response says neither.
    scope: str | None = None
    failures: int = 0

    def seconds_remaining(self, *, now: datetime | None = None) -> float:
        if self.until is None:
            return 0.0
        now = now or datetime.now(UTC)
        return max(0.0, (self.until - now).total_seconds())


def record_attempt(
    connection: Connection,
    *,
    email: str,
    succeeded: bool,
    user_id: UUID | None = None,
    reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
) -> None:
    """Append one attempt. Both outcomes, always.

    A table holding only failures answers "was this attacked" and cannot
    answer "was this attacked *successfully*", which is the question an
    incident review starts with.
    """
    connection.execute(
        login_attempts.insert().values(
            email=normalise_email(email),
            user_id=user_id,
            succeeded=succeeded,
            reason=reason,
            ip_address=ip_address,
            user_agent=user_agent,
            attempted_at=at or datetime.now(UTC),
        )
    )


def lockout_state(
    connection: Connection,
    *,
    email: str,
    ip_address: str | None = None,
    settings: IdentitySettings | None = None,
    now: datetime | None = None,
) -> Lockout:
    """Whether this email — or this source — is currently locked out.

    Checked before the password is verified, so a locked account costs an
    attacker a cheap refusal rather than an argon2 verification. That is
    also why the refusal must not depend on whether the account exists:
    the check runs identically for an address that names nothing.
    """
    settings = settings or IdentitySettings()
    now = now or datetime.now(UTC)
    window_start = now - timedelta(seconds=settings.lockout_window_seconds)

    by_email = _recent_failures(
        connection,
        column=login_attempts.c.email,
        value=normalise_email(email),
        window_start=window_start,
    )
    if by_email.count >= int(settings.max_failed_attempts):
        return Lockout(
            locked=True,
            until=by_email.latest + timedelta(seconds=settings.lockout_seconds),
            scope="email",
            failures=by_email.count,
        )

    if ip_address:
        by_address = _recent_failures(
            connection,
            column=login_attempts.c.ip_address,
            value=ip_address,
            window_start=window_start,
        )
        if by_address.count >= int(settings.max_failed_attempts_per_address):
            return Lockout(
                locked=True,
                until=by_address.latest + timedelta(seconds=settings.lockout_seconds),
                scope="address",
                failures=by_address.count,
            )

    return Lockout(locked=False, failures=by_email.count)


@dataclass(frozen=True, slots=True)
class _Failures:
    count: int
    latest: datetime


def _recent_failures(
    connection: Connection, *, column, value: str, window_start: datetime
) -> _Failures:
    """Failures since the last success for this key, inside the window.

    "Since the last success" is what replaces the counter an append-only
    table cannot have. A successful login therefore clears the count
    without erasing the history — the failures stay on the record for
    whoever reads this table after an incident.
    """
    last_success = connection.execute(
        select(func.max(login_attempts.c.attempted_at)).where(
            and_(column == value, login_attempts.c.succeeded.is_(True))
        )
    ).scalar_one_or_none()

    floor = window_start if last_success is None else max(window_start, last_success)

    row = connection.execute(
        select(
            func.count().label("count"),
            func.max(login_attempts.c.attempted_at).label("latest"),
        ).where(
            and_(
                column == value,
                login_attempts.c.succeeded.is_(False),
                login_attempts.c.attempted_at > floor,
            )
        )
    ).one()

    return _Failures(count=int(row.count or 0), latest=row.latest or window_start)


def recent_attempts(
    connection: Connection, *, email: str, limit: int = 20
) -> list[dict[str, object]]:
    """The most recent attempts against one address. For an operator, not a caller."""
    rows = connection.execute(
        select(login_attempts)
        .where(login_attempts.c.email == normalise_email(email))
        .order_by(desc(login_attempts.c.attempted_at))
        .limit(limit)
    ).all()
    return [
        {
            "succeeded": row.succeeded,
            "reason": row.reason,
            "ip_address": row.ip_address,
            "attempted_at": row.attempted_at,
        }
        for row in rows
    ]
