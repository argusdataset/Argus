"""Mounts ARGUS Public's static frontend onto the public_stats service.

The frontend brief was explicit: prefer a relative/internal path over a
new cross-origin call, and follow how Modules 19-22 are already served on
Railway rather than inventing a second deploy target. There is no
existing "frontend serving" pattern to follow — Modules 19-22 are pure
JSON APIs — but there *is* an existing pattern for turning one of their
`create_app()` results into something deployable without editing the
service's own source: `infra/deploy/asgi.py` already wraps the returned
FastAPI app in `LivenessMiddleware` and `TlsPolicyMiddleware` by
composition. This module does the same thing, at the same layer, for the
same reason — a deployment concern added around a service rather than
into it.

## Why this makes the frontend same-origin with zero CORS configuration

`web/public/app.js` calls `/public/stats`, not `https://something/public/
stats`. Mounting the static files onto the *same* FastAPI app that
answers those paths means the browser never leaves the origin it loaded
the page from, so there is no cross-origin request to configure `harden`
for. That is the whole reason this mounts onto `public_stats` specifically
rather than becoming its own Railway service: a second service would have
its own domain, and calling Module 20's API from it would be exactly the
new cross-origin path the brief said to avoid.

## Why this does not touch `services/public_stats/`

`create_app()` returns a real `FastAPI` instance with its API routes
already registered — `harden()` adds middleware and returns the same
object, it does not wrap it in a different type. `app.mount("/", ...)`
called from here, after `create_app()` has returned, appends one more
route to that same instance. Starlette matches routes in registration
order and stops at the first match, so `/public/stats` and
`/public/releases` — registered first, inside `create_app()` — are found
before the mount is ever consulted; the mount only catches paths nothing
else claimed. Zero lines of Module 20's source change, and a diff of this
module proves it.

## Why it can be deleted without touching the backend

Two things point at each other: this file, and one line in
`infra/deploy/asgi.py`'s `_public_stats` factory. Removing both, plus the
`web/public/` directory, returns `public_stats` to exactly what Module 20
built — a JSON API with no static assets — which is the whole point of
keeping the frontend "its own directory/module" the brief asked for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["FRONTEND_CSP", "WEB_PUBLIC_DIR", "mount_public_web"]

#: `infra/deploy/public_web.py` -> `infra/deploy` -> `infra` -> repo root -> `web/public`.
WEB_PUBLIC_DIR = Path(__file__).resolve().parents[2] / "web" / "public"

#: `infra/security/headers.py` sets `default-src 'none'` for every response
#: in this app, and says explicitly why: that is the correct policy for
#: an API serving no HTML, script or styling, and "a future browser UI
#: needs its own, much more permissive policy and would set one on its
#: own responses." This is that policy — set here, for the frontend's own
#: responses only, rather than by changing Module 24's default.
#:
#: Still same-origin only. No `unsafe-inline`, no `unsafe-eval`, no
#: external host of any kind: `app.js` and `style.css` are the only two
#: resources this page ever loads, both served from this origin, and
#: neither needs an exemption from CSP — only a policy that permits
#: loading same-origin resources at all. `style-src` carries no
#: `unsafe-inline` on purpose; see `app.js`'s `svgBarFill` for how the
#: one place that would have wanted an inline style avoids needing one.
FRONTEND_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)


class _CspOverride:
    """Replaces whatever CSP the wrapped app's response carries with `FRONTEND_CSP`.

    `SecurityHeadersMiddleware` (Module 24) stamps its own
    `Content-Security-Policy` with `response.headers.setdefault(...)` —
    it fills the header in only when a response does not already have
    one. Setting it here, inside the static mount and therefore upstream
    of that middleware in the response path, is what makes `FRONTEND_CSP`
    the value `setdefault` finds already present and leaves alone. Pure
    ASGI, matching the `_with_hsts` pattern in `infra/deploy/tls.py`,
    for the same reason: wrapping `send` is the layer this can be done at
    without touching the app it wraps.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def _send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"content-security-policy"
                ]
                headers.append((b"content-security-policy", FRONTEND_CSP.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, _send)


def mount_public_web(app: FastAPI, *, directory: Path | None = None) -> FastAPI:
    """Serve `web/public`'s static files from `app`, at `/`. Returns `app`.

    Raises if the directory is missing rather than silently skipping the
    mount — a public_stats container that boots without its frontend
    should fail loudly, not serve a working API behind a blank page with
    no indication anything is wrong.
    """
    target = directory or WEB_PUBLIC_DIR
    if not target.is_dir():
        raise RuntimeError(
            f"web/public not found at {target}. The public_stats image must include it — "
            "see the Dockerfile's `COPY web/ ./web/` step."
        )
    static = StaticFiles(directory=str(target), html=True)
    app.mount("/", _CspOverride(static), name="public_web")
    return app
