"""`harden(app)`. One call, added once per service, wiring everything in this
package onto a FastAPI app: CORS, security headers, and the general
request-rate ceiling.

The reason this is a single function rather than three separate calls
each service repeats is that Module 22 through Module 23 built four
independent `create_app`s with no shared middleware convention, and
copy-pasted middleware setup is exactly the kind of thing that quietly
diverges — one service gets the rate limiter and forgets a header, a
second gets both but with different CORS kwargs. `harden` is the single
place that convention lives, so all four services get the same policy
from the same code rather than four hand-maintained copies of it.

Order matters and is fixed here rather than left to call order at each
site: Starlette applies middleware in the reverse of the order it is
added, so the rate limiter is added **last** to run **first** — a
request over the ceiling should never reach CORS negotiation or a route,
both of which do more work than a counter increment.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from infra.security.config import SecurityConfig
from infra.security.headers import SecurityHeadersMiddleware, cors_kwargs
from infra.security.rate_limit import RateLimitMiddleware

__all__ = ["harden"]


def harden(app: FastAPI, *, security: SecurityConfig | None = None) -> FastAPI:
    """Attach CORS, security headers, and the general rate ceiling. Returns `app`."""
    config = security or SecurityConfig()
    app.state.security = config

    app.add_middleware(CORSMiddleware, **cors_kwargs(config.settings))
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware, settings=config.settings)
    return app
