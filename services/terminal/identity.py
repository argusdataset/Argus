"""Who is asking — a deliberately temporary stub, shaped for Module 22 to replace.

## The whole point of this file is that it is one function

Module 22 (Authentication) is not built. Watchlists need an owner anyway.
The failure mode to avoid is identity leaking into every route as an
implicit convention, so that when real auth arrives it has to be threaded
through a dozen call sites.

So: `current_user_id` is the only place in this service that decides who
is asking. Every user-scoped route depends on it. Module 22 replaces its
body — verify a session token instead of trusting a header — and touches
nothing else. No route signature changes, no query changes, no schema
changes.

## The stub trusts a header, which is an authentication bypass

Saying that plainly is the point. `X-Argus-User` carrying a user id is
not authentication; anyone who can reach the service can be anyone. It is
acceptable *only* because Module 22 has not defined its patterns yet and
guessing at them would produce a half-built auth system, which the module
brief rules out explicitly.

Two things stop it shipping by accident:

**It is a config flag, defaulting to on but checkable.** With
`stub_identity_enabled=False` every user-scoped endpoint returns 501 and
names Module 22. Turning the stub off is therefore a one-line deployment
change, not a code change, and a deployment that forgets is a deployment
that *left it on* rather than one that failed to remove it.

**It resolves against a real `users` row.** The header is not taken at
face value as an opaque string: it must be a UUID naming a row that
exists, or the request is refused. Watchlist ownership is therefore
genuinely foreign-keyed from day one, and Module 22 inherits real rows
rather than a pile of strings that have to be reconciled.

What it does *not* do is check a password, a token, a signature, or a
session. It is a stand-in for identity, not for authentication, and the
difference is the entire security model here.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from infra.db.schema.users import users
from services.terminal.config import TerminalConfig
from services.terminal.errors import (
    IDENTITY_REQUIRED,
    IDENTITY_UNAVAILABLE,
    TerminalError,
)

__all__ = ["USER_HEADER", "current_user_id", "stub_user_exists"]

#: The header the stub reads. Named with an `X-Argus-` prefix so it is
#: obviously ours and obviously not a standard auth mechanism.
USER_HEADER = "X-Argus-User"


def current_user_id(
    connection: Connection,
    raw_header: str | None,
    *,
    config: TerminalConfig | None = None,
) -> UUID:
    """The user making this request, or a `TerminalError` explaining why not.

    Module 22 replaces this function. The contract it must keep: return a
    `UUID` naming a row in `users`, or raise `TerminalError`. Everything
    downstream — every watchlist query, every ownership check — is written
    against that and nothing else.
    """
    config = config or TerminalConfig()

    if not config.stub_identity_enabled:
        raise TerminalError(
            IDENTITY_UNAVAILABLE,
            "This deployment cannot establish who you are: the development "
            "identity stub is disabled and Module 22 (Authentication) is not built. "
            "No user-scoped endpoint can be served.",
            status=501,
        )

    if not raw_header:
        raise TerminalError(
            IDENTITY_REQUIRED,
            f"This endpoint is user-scoped and no identity was supplied. Send the "
            f"{USER_HEADER} header. Note that this is a development stub, not "
            f"authentication — see services/terminal/identity.py.",
            status=401,
            detail={"header": USER_HEADER},
        )

    try:
        user_id = UUID(raw_header.strip())
    except ValueError as error:
        raise TerminalError(
            IDENTITY_REQUIRED,
            f"{USER_HEADER} must be a user UUID; got {raw_header!r}.",
            status=401,
            detail={"header": USER_HEADER},
        ) from error

    if not stub_user_exists(connection, user_id):
        # Refused rather than trusted. Ownership is foreign-keyed, so a
        # watchlist created under a made-up id would fail at the database
        # anyway — failing here says why, and keeps the stub from being a
        # way to invent users.
        raise TerminalError(
            IDENTITY_REQUIRED,
            f"No user {user_id} exists.",
            status=401,
            detail={"header": USER_HEADER, "user_id": str(user_id)},
        )

    return user_id


def stub_user_exists(connection: Connection, user_id: UUID) -> bool:
    return (
        connection.execute(select(users.c.id).where(users.c.id == user_id)).scalar_one_or_none()
        is not None
    )
