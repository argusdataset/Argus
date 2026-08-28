"""The HTTP surface for Module 23's `check_health`. Split by audience.

Module 23 built `check_health()` — ok/degraded/down, database, migrations,
data freshness — as a pure function taking an engine or a connection.
Nothing wired it to an HTTP route: this is that wiring, and the split
Module 24's hardening pass asks for.

## Two audiences, two endpoints, two amounts of detail

**`GET /health/live`** — unauthenticated, minimal. A process orchestrator
asking "should I restart this" needs one bit — is the process able to
talk to its database — and nothing else. It does not report which feed
is stale, what the migration revision is, or any other structured detail,
because none of that changes what a liveness probe does with the answer,
and all of it is reconnaissance value handed to an unauthenticated
caller for free: the schema version in use, which data feeds exist,
which of them are behind. A liveness probe is a status, not a report.

**`GET /health/detail`** — gated behind Module 22's identity seam,
`admin`-level, matching the pattern `services/identity/app.py`'s own
admin router already established (`require_role(connection, actor,
ADMIN)`, which also demands an enrolled second factor — see
`services/identity/roles.py`). Returns everything `check_health` knows:
per-check status, migration revision, per-feed freshness state. The
detail is exactly what an operator debugging a degraded system needs and
exactly what Module 24's Part 0 flagged as reconnaissance value in the
wrong hands.

## Why this is its own small app rather than a router on an existing one

Health is a whole-system concept — the database, the schema, every
canonical feed — not any one service's concern, and none of Modules
19-22 owns it. A router grafted onto one of them would make that
service's health page describe the whole system, which is a strange
thing for, say, the Terminal to be responsible for. A dedicated app
keeps the boundary honest: this is infrastructure, mountable wherever a
deployment wants it, not a feature of any one product surface.

Authentication for the detail route calls straight into
`services.identity.seam.resolve_identity` and `services.identity.roles.
require_role` — the same functions Module 22 built the seam from —
rather than reimplementing a second bearer-token check. There is exactly
one way to prove you are an admin in ARGUS, and this is not a second one.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from infra.observability.health import check_health
from infra.security.config import SecurityConfig
from infra.security.middleware import harden
from services.identity.errors import IDENTITY_REQUIRED, IdentityError
from services.identity.roles import ADMIN, require_role
from services.identity.seam import resolve_identity
from services.identity.tokens import bearer_token

__all__ = ["create_health_app", "get_connection"]


def create_health_app(engine: Engine, *, security: SecurityConfig | None = None) -> FastAPI:
    app = FastAPI(
        title="ARGUS Health",
        version="1",
        summary="Liveness for orchestrators; full detail for admins.",
    )
    app.state.engine = engine

    @app.exception_handler(IdentityError)
    async def _error(_request: Request, error: IdentityError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content=error.payload())

    @app.get("/health/live")
    def live() -> JSONResponse:
        """Up or down. Nothing else. Never authenticated — see the module docstring.

        Calls `check_health(engine)` directly rather than going through
        `get_connection` — deliberately, and unlike `detail` below.
        `check_health`'s engine path wraps `engine.connect()` in its own
        `except SQLAlchemyError`, so a completely unreachable database
        still produces a clean `down` rather than a raised exception.
        Reaching this through a FastAPI dependency would call
        `engine.connect()` a second time, outside that protection, and a
        request against a dead database would 500 instead of correctly
        reporting itself down — a liveness probe's one job is not to do
        that. `alembic_version` and `SELECT 1` are stable facts about the
        committed database, not about any one request's transaction, so
        nothing here needs the connection a test's fixtures write
        through — a fresh connection off the real engine sees them fine.
        """
        report = check_health(engine)
        # Deliberately not report.as_dict(): that carries every check's
        # detail, and a liveness probe gets only the one bit an
        # orchestrator acts on.
        body = {"status": "down" if report.status.value == "down" else "up"}
        return JSONResponse(status_code=report.http_status, content=body)

    @app.get("/health/detail")
    def detail(
        connection: Annotated[Connection, Depends(get_connection)],
        actor_id: Annotated[object, Depends(_current_admin)],
    ) -> JSONResponse:
        """Everything `check_health` knows. Requires `admin` and an enrolled second factor.

        Checked against the *same* connection the admin proof used —
        FastAPI caches a dependency's result per request, so this is one
        connection, not two — rather than a second one opened fresh
        against `engine`. In production the two are equivalent; in a test
        whose fixtures write through an overridden, transaction-scoped
        connection, a second real connection would not see them.
        """
        report = check_health(connection=connection)
        return JSONResponse(status_code=report.http_status, content=report.as_dict())

    return harden(app, security=security)


def get_connection(request: Request) -> Iterator[Connection]:
    engine: Engine = request.app.state.engine
    with engine.connect() as connection:
        yield connection


def _current_admin(
    request: Request,
    connection: Annotated[Connection, Depends(get_connection)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> object:
    """Prove the caller is a signed-in admin, through Module 22's own machinery.

    Calls `resolve_identity` and `require_role` directly rather than
    depending on any one service's `create_app` internals — this app has
    no service of its own to borrow a dependency from, and duplicating a
    bearer-token check here would be the second identity mechanism Module
    19 built the seam specifically to avoid.
    """
    if not bearer_token(authorization):
        raise IdentityError(
            IDENTITY_REQUIRED,
            "The detailed health view needs a session. Send 'Authorization: Bearer <token>'.",
            status=401,
            detail={"scheme": "Bearer"},
        )

    user_id, _mechanism = resolve_identity(
        connection,
        None,
        authorization=authorization,
        stub_enabled=False,
        user_header_name="X-Argus-User",
    )
    if user_id is None:
        raise IdentityError(
            IDENTITY_REQUIRED,
            "This session is not valid. Sign in again.",
            status=401,
        )

    require_role(connection, user_id, ADMIN)
    return user_id
