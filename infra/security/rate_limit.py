"""A general request-rate ceiling, at the ASGI layer, in front of every route.

## What this is and is not

Module 22 built brute-force protection for one thing: credential
guessing, counted from an append-only log, per email and per source,
surviving a process restart. That mechanism is exactly right for what it
defends and is not touched here.

This is different and smaller: a burst ceiling on **raw request volume**
per source address, regardless of which endpoint or whether the request
even reaches a route. It exists to catch a runaway client or a naive
scraping burst before it does real work — a database round trip, an
argon2 hash — rather than to be a precise, persistent record of abuse. In
memory, per process, reset on restart, and that trade is deliberate: a
database write on every single request in order to rate-limit requests
would be the rate limiter becoming the load problem it exists to prevent.

## Fixed window, not sliding, and why that is an acceptable simplification

A fixed window can admit up to double the configured rate across a window
boundary (a burst at the end of one window, another at the start of the
next). A sliding window or a token bucket avoids that at the cost of more
bookkeeping per request. For a coarse burst ceiling sitting in front of
Module 22's precise, persistent, identity-aware limiting, the fixed
window's simplicity is worth the imprecision — this is the blunt
instrument, not the scalpel.

## Single-process, and what that means for a real deployment

The counters here are a plain `dict` on the middleware instance. Running
ARGUS as more than one process (multiple workers, multiple containers)
gives every process its own independent ceiling, so the *effective* limit
scales with process count. That is a real limitation, not a
documentation footnote: closing it needs a shared store (Redis is the
obvious choice) reachable from every process, which is an infrastructure
decision for whoever configures the deployment topology — flagged for
Module 25 rather than solved here with an in-process approximation dressed
up as a distributed one.

## Client IP, trusted-proxy aware

Uses `resolve_client_ip`, the same function `services/identity/app.py`'s
lockout now uses, so a deployment's trusted-proxy configuration governs
both consistently — one setting, not two that could disagree.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from infra.security.client_ip import resolve_client_ip
from infra.security.config import SecuritySettings
from infra.security.headers import SECURITY_HEADERS
from services.shared.errors import error_payload

__all__ = ["RATE_LIMITED", "RateLimitMiddleware"]

RATE_LIMITED = "RATE_LIMITED"

#: Paths never subject to the general ceiling. A liveness probe that can
#: itself be rate-limited into reporting "down" is a monitoring system
#: that lies exactly when it is being hammered — the moment it matters
#: most.
_EXEMPT_PATHS: frozenset[str] = frozenset({"/health/live"})


@dataclass(slots=True)
class _Window:
    started_at: float
    count: int = 0
    blocked_until: float | None = None


class RateLimitMiddleware:
    """Pure ASGI middleware: no FastAPI dependency graph, so it runs before routing.

    A `Depends`-based limiter only fires once FastAPI has matched a route,
    which means an unmatched or malformed request — the shape a scanning
    tool sends — pays the cost of routing before being refused. ASGI
    middleware sits in front of that, at the layer a burst should be
    stopped.
    """

    def __init__(self, app: ASGIApp, *, settings: SecuritySettings | None = None) -> None:
        self._app = app
        self._settings = settings or SecuritySettings()
        self._windows: dict[str, _Window] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return

        request = _ScopeRequest(scope)
        address = resolve_client_ip(request, self._settings) or "unknown"
        now = time.monotonic()

        blocked_for = self._register(address, now)
        if blocked_for is not None:
            response = JSONResponse(
                status_code=429,
                content=error_payload(
                    RATE_LIMITED,
                    "Too many requests from this address. Slow down and retry shortly.",
                    detail={"retry_after_seconds": round(blocked_for, 1)},
                ),
                # `SECURITY_HEADERS` too, not just `Retry-After`. This
                # middleware sits outside `SecurityHeadersMiddleware` in
                # the stack — that is the whole point, so a refusal never
                # pays for CORS negotiation or routing — but its own
                # response would otherwise be the one response in the
                # entire app that skips the standard headers. "Every
                # response carries these" should not have an asterisk for
                # the response most likely to come from an adversarial
                # client.
                headers={
                    "Retry-After": str(max(1, int(blocked_for))),
                    **SECURITY_HEADERS,
                },
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)

    def _register(self, address: str, now: float) -> float | None:
        """Record one request. Returns seconds-to-wait if it must be refused."""
        window_seconds = self._settings.request_rate_window_seconds.value
        ceiling = int(self._settings.request_rate_limit)
        block_seconds = self._settings.rate_limit_block_seconds.value

        window = self._windows.get(address)
        if window is None or now - window.started_at >= window_seconds:
            window = _Window(started_at=now)
            self._windows[address] = window

        if window.blocked_until is not None:
            if now < window.blocked_until:
                return window.blocked_until - now
            # The block has expired; start counting fresh rather than
            # carrying the count that caused it forward.
            window = _Window(started_at=now)
            self._windows[address] = window

        window.count += 1
        if window.count > ceiling:
            window.blocked_until = now + block_seconds
            return block_seconds
        return None


@dataclass(slots=True)
class _ScopeRequest:
    """The narrow slice of `Request` that `resolve_client_ip` needs.

    Built from the raw ASGI scope rather than a full `starlette.Request`
    so this middleware has no dependency on request parsing having
    happened — it runs before FastAPI's request object would normally
    exist.
    """

    scope: dict[str, Any]

    @property
    def client(self) -> Any:
        client = self.scope.get("client")
        return _Client(client[0]) if client else None

    @property
    def headers(self) -> _Headers:
        return _Headers(self.scope.get("headers") or ())


@dataclass(slots=True)
class _Client:
    host: str


@dataclass(slots=True)
class _Headers:
    raw: Any = field(default_factory=tuple)

    def get(self, name: str) -> str | None:
        target = name.lower().encode("latin-1")
        for key, value in self.raw:
            if key.lower() == target:
                return value.decode("latin-1")
        return None
