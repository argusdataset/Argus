"""Who is asking. Module 22 replaced this function's body; the contract held.

## What this file promised, and what actually happened

Module 19 wrote this file as a stub and made one promise about it:

> "Module 22 replaces its body — verify a session token instead of
> trusting a header — and touches nothing else. No route signature
> changes, no query changes, no schema changes. Its contract is exactly:
> return a `UUID` naming a row in `users`, or raise `TerminalError`."

That is what happened. `current_user_id` now verifies a session through
`services.identity.seam.resolve_identity`. Its signature gained one
keyword argument with a default, so every existing caller compiles and
behaves identically; no route in Modules 19, 20 or 21 changed, no query
changed, no schema changed, and their test suites pass unmodified.

The stub is still here, and that is deliberate — see below.

## Two ways in, and only one of them is authentication

**A session.** `Authorization: Bearer <token>`, verified against
`sessions` by Module 22: the hash matches, it has not expired, it has not
been revoked, and the account is still active. This path is never gated
by a configuration flag, because a deployment must not be able to switch
authentication off.

**The header stub.** `X-Argus-User` carrying a user id is *not*
authentication — anyone who can reach the service can be anyone. It
survives only as a local-development affordance, is available only while
`stub_identity_enabled` is true, and every request it serves is logged at
WARNING naming the bypass and the user it granted.

An invalid session token does not fall back to the stub. A request that
presents a credential and has it rejected is refused, not re-served as
whoever its header named — that fallback would be a privilege escalation
wearing the clothes of a convenience.

## What `stub_identity_enabled` means now

It changed from "can this service answer user-scoped requests at all" to
"is the bypass available". The reasoning is in
`services/identity/seam.py`; the short version is that the flag now
closes the bypass instead of closing the service, so turning it off is
something a deployment can actually do. `False` still produces a 501, but
only for the request that genuinely cannot be answered: no credential
supplied, and no stub to fall back on.

Production posture is `stub_identity_enabled=False`. The default stayed
`True` so that Modules 19-21's existing suites, written against the stub,
keep passing without modification — which is the evidence the seam held.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from infra.db.schema.users import users
from services.identity.seam import resolve_identity
from services.identity.tokens import bearer_token
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
    authorization: str | None = None,
) -> UUID:
    """The user making this request, or a `TerminalError` explaining why not.

    The contract Module 19 specified and Module 22 kept: return a `UUID`
    naming a row in `users`, or raise `TerminalError`. Everything
    downstream — every watchlist query, every ownership check — is written
    against that and nothing else.

    `authorization` is the standard header carrying the session token. It
    is keyword-only with a default so that Module 19-era callers passing
    only `(connection, raw_header, config=...)` still work exactly as they
    did; the two dependency bodies that can supply it now do.
    """
    config = config or TerminalConfig()

    user_id, _mechanism = resolve_identity(
        connection,
        raw_header,
        authorization=authorization,
        stub_enabled=config.stub_identity_enabled,
        user_header_name=USER_HEADER,
    )
    if user_id is not None:
        return user_id

    # Nobody is signed in. Which refusal depends on *which mechanism* the
    # caller reached for, not on whether they supplied something:
    #
    #   no Bearer token, stub off  -> 501. The caller used a mechanism this
    #                                 deployment does not offer — either the
    #                                 header, or nothing at all. Module 19
    #                                 chose this status for exactly this
    #                                 case and the reasoning survives real
    #                                 auth: the caller did nothing wrong.
    #   anything else              -> 401. A credential was presented
    #                                 through a mechanism that exists, and
    #                                 it did not check out.
    if not bearer_token(authorization) and not config.stub_identity_enabled:
        raise TerminalError(
            IDENTITY_UNAVAILABLE,
            f"This deployment cannot establish who you are from what you sent: the "
            f"development identity stub is disabled, so the {USER_HEADER} header is "
            f"not accepted. Module 22 (Authentication) provides session-based "
            f"identity — sign in at /identity/login and send the session token as "
            f"'Authorization: Bearer <token>'.",
            status=501,
            detail={"scheme": "Bearer", "stub_enabled": False},
        )

    raise TerminalError(
        IDENTITY_REQUIRED,
        f"This endpoint is user-scoped and no valid session was supplied. Sign in at "
        f"/identity/login and send the session token as "
        f"'Authorization: Bearer <token>'. ({USER_HEADER} is a development stub, not "
        f"authentication — see services/terminal/identity.py.)",
        status=401,
        detail={"scheme": "Bearer", "header": USER_HEADER},
    )


def stub_user_exists(connection: Connection, user_id: UUID) -> bool:
    return (
        connection.execute(select(users.c.id).where(users.c.id == user_id)).scalar_one_or_none()
        is not None
    )
