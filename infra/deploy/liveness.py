"""A liveness probe on every deployed container, added without touching a service.

Railway health-checks the container it just started and refuses to route
traffic to one that does not answer. So the probe has to exist on *each*
service: a separate health service can say the database is reachable, but
it cannot say whether the Terminal container that just booted is able to
serve, which is the only question a deployment health check asks.

Modules 19 through 22 have no such route, and adding one to their
`create_app` would be changing their application logic — outside this
module's boundary and the wrong place besides, since "answer a platform's
health poll" is a fact about being deployed rather than a feature of the
Terminal.

So it is added here, by composition: a small ASGI middleware that answers
`/health/live` itself and delegates everything else. Every service gets
it for free through `asgi.py`, and the health service — which already has
the route from Module 24 — is left to answer with its own, because this
middleware only claims a path the wrapped app does not serve.

## What "alive" means, and why it is not just "the process is running"

Answering 200 as soon as the process starts would make the probe useless:
a container that booted, failed to reach its database, and is returning
500 to every real request would be reported healthy and kept in
rotation.

So the probe runs Module 23's `check_health` and reports `down` when that
does — which covers an unreachable database and a schema at the wrong
revision. Both are conditions under which this container cannot correctly
serve and traffic should go elsewhere.

A **degraded** instance answers 200 and stays in rotation, exactly as
Module 24 decided: a stale data feed is not fixed by having fewer
servers, and removing them makes the outage worse.

## Why it does not call the database on every poll without limit

A platform polls this every few seconds, per container. `check_health`
is three cheap queries, but three queries times five services times a
poll every five seconds is a steady background load that exists only to
answer a question whose answer rarely changes. So the result is cached
for a few seconds — long enough to collapse a poll storm, short enough
that a database going away is noticed within one poll interval.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import Engine
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from infra.observability.health import check_health
from infra.security.headers import SECURITY_HEADERS

__all__ = ["LIVENESS_PATH", "LivenessMiddleware", "serves_liveness"]

LIVENESS_PATH = "/health/live"

#: How long a liveness verdict is reused. Under a platform polling every
#: few seconds this collapses a burst into one database round trip;
#: above it, a database that has gone away is noticed on the next poll.
_CACHE_SECONDS = 3.0


class LivenessMiddleware:
    """Answers `/health/live` for a service that does not serve it itself.

    Deliberately checks whether the wrapped app already has the route —
    Module 24's health service does — and delegates rather than shadowing
    it. Two definitions of liveness answering on the same path, with the
    outer one winning, is the kind of thing that is invisible until the
    two disagree.
    """

    def __init__(self, app: ASGIApp, *, engine: Engine, claim_path: bool = True) -> None:
        self._app = app
        self._engine = engine
        self._claim_path = claim_path
        self._cached: tuple[float, int, dict[str, str]] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._claim_path or scope["type"] != "http" or scope["path"] != LIVENESS_PATH:
            await self._app(scope, receive, send)
            return

        status, body = self._verdict()
        response = JSONResponse(status_code=status, content=body, headers=dict(SECURITY_HEADERS))
        await response(scope, receive, send)

    def _verdict(self) -> tuple[int, dict[str, str]]:
        now = time.monotonic()
        if self._cached is not None and now - self._cached[0] < _CACHE_SECONDS:
            return self._cached[1], self._cached[2]

        report = check_health(self._engine)
        status = report.http_status
        body = {"status": "down" if report.status.value == "down" else "up"}

        self._cached = (now, status, body)
        return status, body


def serves_liveness(app: Any) -> bool:
    """Whether an app already routes `LIVENESS_PATH` itself.

    Read from the app's own routes rather than from a hardcoded list of
    which services have one, so a service that gains the route later
    stops being wrapped without anybody remembering to update this.
    """
    routes = getattr(app, "routes", None)
    if not routes:
        return False
    return any(getattr(route, "path", None) == LIVENESS_PATH for route in routes)
