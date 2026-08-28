"""CORS and standard security response headers.

## Why this exists as one small file rather than being left to each service

Modules 19-22 each built a `create_app` with no CORS configuration and no
security headers at all — which was the correct amount of scope for each
of them: none had a browser client to serve, so there was nothing to
configure yet. That default has a name once the whole surface is
considered together, and it is not a good one: FastAPI without
`CORSMiddleware` configured refuses cross-origin browser requests by
omission rather than by policy, which means the day someone adds a
front-end, the *first* thing they will do is find the fastest CORS
configuration that makes the error go away — usually `allow_origins:
["*"]`. Shipping an explicit, narrow default now is cheaper than fixing
whatever gets reached for under deadline pressure later.

## CORS: closed by default, and why that is still useful with no UI yet

`cors_kwargs()` returns an empty `allow_origins` list unless the
deployment configures one. An API with no allowed origins still needs
`CORSMiddleware` present, because its absence is not "closed", it is
"undefined" — a browser is left to its own default same-origin behaviour,
which is *usually* equivalent but is not a policy ARGUS stated anywhere.
Configuring it, even to nothing, is the difference between "no origins
are allowed because none were configured for this deployment" and "no
origins are allowed because nobody has looked at this yet."

## The headers, and why each one

- `X-Content-Type-Options: nosniff` — stops a browser guessing a
  response's type from its content. ARGUS serves JSON; nothing here
  should ever be interpreted as HTML or a script.
- `Referrer-Policy: no-referrer` — a URL in this API can carry a ticker,
  a setup id, or a session-adjacent path segment. Nothing downstream a
  browser might link to needs to receive it.
- `X-Frame-Options: DENY` — ARGUS has no UI to embed and no reason to be
  framed; refusing outright is simpler than reasoning about which sites
  might legitimately want to.
- `Content-Security-Policy: default-src 'none'` — the correct policy for
  an API that serves no HTML, no script, and no styling: nothing on this
  origin should ever execute. A future browser UI needs its own, much
  more permissive policy and would set one on its own responses; this is
  groundwork for the API surface, not a policy meant to survive a UI
  being added to the same origin.
- `Permissions-Policy` — turns off browser features (camera, microphone,
  geolocation) that a JSON API has no use for and that cost nothing to
  disable.

**Deliberately not set: `Strict-Transport-Security`.** HSTS is a promise
that this origin is reachable over HTTPS, kept for as long as a browser
remembers the header — sending it from a service that might be reached
over plain HTTP during development, or before TLS termination is wired
up in front of it, would be issuing a promise this layer cannot keep and
that a browser would hold the deployment to regardless. TLS is deferred
to Module 25 (see the report); HSTS belongs beside that decision, set at
the point that actually terminates TLS, not here.
"""

from __future__ import annotations

from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from infra.security.config import SecuritySettings

__all__ = ["SECURITY_HEADERS", "SecurityHeadersMiddleware", "cors_kwargs"]

#: Applied to every response, success or error alike — a security header
#: on the happy path only is a security header an error response defeats.
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamps `SECURITY_HEADERS` onto every response this app returns."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


def cors_kwargs(settings: SecuritySettings | None = None) -> dict[str, Any]:
    """Keyword arguments for Starlette's `CORSMiddleware`.

    `allow_origins` is empty unless explicitly configured — no UI exists
    yet, so there is nothing to allow, and `["*"]` is never the answer
    for an API that carries session tokens: the CORS specification itself
    refuses to combine a wildcard origin with credentialed requests, and
    `allow_credentials=True` is set here because `Authorization` is
    exactly the credential this API's browser clients would eventually
    send.
    """
    settings = settings or SecuritySettings()
    return {
        "allow_origins": list(settings.cors_allowed_origins),
        "allow_credentials": True,
        "allow_methods": ["GET", "POST", "PATCH", "DELETE"],
        "allow_headers": ["Authorization", "Content-Type", "X-Argus-User"],
    }
