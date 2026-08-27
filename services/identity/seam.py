"""The new body of Module 19's `current_user_id`. Session verification.

Module 19 wrote the contract this file has to keep, and wrote it as a
promise about what Module 22 would *not* need to change:

> "`current_user_id` is the only place identity enters the service. One
> function, one dependency, every user-scoped route behind it. Module 22
> replaces its body — verify a session instead of trusting a header — and
> touches nothing else. No route signature, no query, no schema change.
> Its contract is exactly: return a `UUID` naming a row in `users`, or
> raise `TerminalError`."

That held. `resolve_identity` below is what `current_user_id` now calls,
and Modules 19, 20 and 21 kept every route signature, every query and
every schema. Their test suites pass unmodified.

## Why the credential moved to `Authorization` and how that cost nothing

A session token belongs in `Authorization: Bearer …`. Proxies, log
scrubbers and client libraries all know that header holds a secret and
know nothing about `X-Argus-User`; putting a bearer token in a custom
header is precisely the sort of thing Module 24 would flag.

Reading a second header looked like it would need a route change, and
did not: Module 19's `get_user` and Module 21's `optional_user` already
take `request: Request` for other reasons, so they read
`request.headers` and pass the value through. Both are *dependency*
bodies, which the brief names as the call sites this module may change.
No route signature moved, and the seam's own signature only gained a
keyword argument with a default — every existing call still compiles and
still behaves identically.

## The stub did not become a bypass; it became a fallback that announces itself

`stub_identity_enabled` meant, in Module 19, "is the header-trusting
stand-in available at all", with False closing user-scoped endpoints
entirely because nothing else could answer. Real auth changes what the
flag is *for* without weakening it:

- **Real sessions are always accepted**, whatever the flag says. Bearer
  verification is not gated by a development switch — a deployment must
  not be able to turn authentication off.
- **The flag now gates only the legacy `X-Argus-User` path.** True means
  a header naming a real user is still accepted, for local development
  and for Module 19/20/21's existing tests; every such request is logged
  at WARNING as an authentication bypass, with the user it granted.
- **False no longer means 501 for everyone.** It means the stub is
  unavailable: a request with a valid session is served normally, and a
  request with only a header is refused. The 501 survives for exactly the
  case that still warrants it — no session and no stub — because that
  request genuinely cannot be answered.

The safety Module 19 built is therefore strengthened rather than removed.
Before, `False` was a kill switch that closed the service; now it is a
kill switch that closes the *bypass* while the service keeps working,
which is the difference between a flag people leave on and a flag people
turn off.

Production posture: `stub_identity_enabled=False`. It stays defaulted to
True only so that Modules 19-21's suites, written against the stub, keep
passing unmodified — which is itself the evidence the seam held.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from infra.db.schema.users import users
from services.identity import sessions
from services.identity.tokens import bearer_token

__all__ = ["STUB_WARNING", "resolve_identity"]

_log = logging.getLogger("argus.identity.seam")

#: Logged on every request served through the header stub. Written as a
#: constant so a test can assert the warning is emitted rather than
#: matching a message string that drifts.
STUB_WARNING = (
    "AUTHENTICATION BYPASS: request identified by the %s header, not by a session. "
    "This is a development affordance and must not be enabled in production. "
    "Granted user %s."
)


def resolve_identity(
    connection: Connection,
    raw_header: str | None,
    *,
    authorization: str | None,
    stub_enabled: bool,
    user_header_name: str,
) -> tuple[UUID | None, str | None]:
    """Who is asking, and how. `(user_id, mechanism)`; `(None, None)` if nobody.

    Returns rather than raises, because the two callers want different
    refusals: Module 19's `get_user` must raise a `TerminalError` naming
    the right code and status, and Module 21's `optional_user` must return
    None for an anonymous caller. Putting the raising in the seam would
    force one of them to catch an exception to express "nobody is signed
    in", which is control flow standing in for a value.

    `mechanism` is "session" or "stub". Callers ignore it; it exists so
    the warning is emitted from one place and so a test can assert which
    path served a request.
    """
    token = bearer_token(authorization)
    if token:
        # Never gated by the stub flag. A deployment must not be able to
        # switch real authentication off.
        user_id = sessions.verify(connection, token)
        if user_id is not None:
            return user_id, "session"
        # A presented-but-invalid token is not silently downgraded to the
        # stub. Falling through to the header here would mean a request
        # with an expired session being served as whoever its header
        # named, which is a privilege escalation dressed as a fallback.
        return None, None

    if not stub_enabled or not raw_header:
        return None, None

    user_id = _stub_user(raw_header)
    if user_id is None or not _exists(connection, user_id):
        return None, None

    _log.warning(STUB_WARNING, user_header_name, user_id)
    return user_id, "stub"


def _stub_user(raw_header: str) -> UUID | None:
    try:
        return UUID(raw_header.strip())
    except ValueError:
        return None


def _exists(connection: Connection, user_id: UUID) -> bool:
    return (
        connection.execute(select(users.c.id).where(users.c.id == user_id)).scalar_one_or_none()
        is not None
    )
