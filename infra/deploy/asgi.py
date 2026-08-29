"""The deployable ASGI applications. One import path per service.

Modules 19 through 22 each built a `create_app(engine, config, security)`
that takes its dependencies as arguments — deliberately, so a test could
hand in a throwaway database. Nothing in the repository ever *called* one
with a real engine, because until this module there was nowhere to run
it. That is the gap this file closes: an importable, uvicorn-addressable
`app` per service, with the engine constructed from configuration and the
deployment profile applied.

## Why four apps and not one

Each service could be mounted under a prefix on a single app. They are
kept separate because they have genuinely different exposure: Public
Stats is unauthenticated by design and expected to take real traffic,
Identity issues credentials, the Terminal and Intelligence serve
authenticated per-user data. One process means one blast radius, one rate
limit budget shared between a public marketing page and a login endpoint,
and one restart taking all four down.

They are also, on Railway, four services with four scaling decisions —
which is the shape the platform wants and the shape the code already
had.

## The composition order, and why it is fixed here

    TlsPolicyMiddleware( LivenessMiddleware( harden( create_app(...) ) ) )

`harden()` is Module 24's and is not modified. TLS policy wraps it from
the outside because a plaintext request carrying a credential should be
refused before Module 24's rate limiter has spent a counter on it and
before CORS negotiation has happened — the same reasoning Module 24 used
for putting its own limiter outermost within `harden`.

`LivenessMiddleware` sits inside the TLS policy and outside the service,
and it is there because Railway will not route traffic to a container
whose health check does not answer. Only the health service has
`/health/live` of its own; the other four would 404 the platform's poll
and never receive traffic. Adding the route to Modules 19-22 would be
changing their application logic, so it is added by composition here —
and the health service, which already serves the path, is wrapped with
`claim_path=False` so its own answer is the one that reaches the
platform.

`public_stats` alone carries one more layer, inside all of the above:
`mount_public_web` (`infra/deploy/public_web.py`) attaches ARGUS Public's
static frontend to the same FastAPI instance `create_app()` returns,
before this file wraps it in liveness and TLS policy like every other
service. Same reasoning as `LivenessMiddleware` — a frontend is a
deployment concern, not application logic, so it is composed on rather
than built into Module 20.

## The engine is created once, at import, per process

Every other `create_engine` call in ARGUS is at the point of use, because
the caller owns the lifecycle (`infra/db/connection.py` says so
explicitly). A web process is the one caller whose lifecycle is the
process: it serves requests until it is killed, and creating a fresh
engine per request would discard the connection pool that makes a pool
worth having.

`pool_pre_ping` is on. A managed Postgres closes idle connections and a
container can sit idle overnight; without it the first request after a
quiet period fails with a stale-connection error that looks like an
outage and is not one.
"""

from __future__ import annotations

import contextlib
import sys
import traceback
from typing import Any

from fastapi import FastAPI
from sqlalchemy import Engine

from infra.db.connection import create_db_engine
from infra.deploy.config import DeploymentProfile, profile_for, security_config_for
from infra.deploy.liveness import LivenessMiddleware, serves_liveness
from infra.deploy.tls import TlsPolicyMiddleware
from infra.observability.logging import configure_logging, get_logger
from infra.security.health_app import create_health_app

__all__ = [
    "SERVICES",
    "build_engine",
    "build_service",
    "health_app",
    "identity_app",
    "intelligence_app",
    "public_stats_app",
    "terminal_app",
]

_log = get_logger("argus.deploy.asgi")


def build_engine(profile: DeploymentProfile) -> Engine:
    """The engine a web process serves from. One per process, pooled.

    `pool_size` is deliberately small. Postgres connections are a scarce,
    server-side resource — a managed instance's limit is in the low
    hundreds — and four services times N workers times a large pool
    exhausts it long before any of them is actually busy. Ten plus five
    overflow per process is generous for a request-serving workload where
    every handler holds its connection for one short transaction.
    """
    return create_db_engine(
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=5,
        pool_recycle=1800,
    )


def build_service(name: str, *, profile: DeploymentProfile | None = None) -> FastAPI | Any:
    """One deployable service, fully composed. See `SERVICES` for the names.

    Validates the deployment profile before building anything. A
    production process configured with a wildcard CORS origin or an
    unshared rate limiter across several workers fails here, at startup,
    with a message naming the variable to fix — rather than serving
    traffic under a policy nobody intended.
    """
    resolved = profile or profile_for()
    resolved.validate()

    security = security_config_for(resolved)
    engine = build_engine(resolved)
    app = SERVICES[name](engine, security=security)
    own_liveness = serves_liveness(app)

    _log.info(
        "service composed",
        extra={
            "event": "service_composed",
            "service": name,
            "environment": resolved.environment.value,
            "hsts": resolved.hsts_enabled,
            "trusted_proxies": len(resolved.trusted_proxies),
            "cors_origins": len(resolved.cors_allowed_origins),
            "workers": resolved.workers,
            "own_liveness": own_liveness,
        },
    )
    probed = LivenessMiddleware(app, engine=engine, claim_path=not own_liveness)
    return TlsPolicyMiddleware(probed, profile=resolved)


def _terminal(engine: Engine, *, security: Any) -> FastAPI:
    from services.terminal.app import create_app

    return create_app(engine, security=security)


def _public_stats(engine: Engine, *, security: Any) -> FastAPI:
    from infra.deploy.public_web import mount_public_web
    from services.public_stats.app import create_app

    return mount_public_web(create_app(engine, security=security))


def _intelligence(engine: Engine, *, security: Any) -> FastAPI:
    from services.intelligence.app import create_app

    return create_app(engine, security=security)


def _identity(engine: Engine, *, security: Any) -> FastAPI:
    from services.identity.app import create_app

    return create_app(engine, security=security)


def _health(engine: Engine, *, security: Any) -> FastAPI:
    return create_health_app(engine, security=security)


#: Every deployable service, by the name its Railway service and its
#: process definition use. Enumerable so a test can assert the set is
#: exactly the services that exist, rather than the ones somebody
#: remembered.
SERVICES: dict[str, Any] = {
    "terminal": _terminal,
    "public_stats": _public_stats,
    "intelligence": _intelligence,
    "identity": _identity,
    "health": _health,
}


#: Written to stderr the instant a factory is entered, before logging is
#: configured and before anything that could fail. Deliberately a bare
#: `print`, not a log record: it has no dependency on `configure_logging`
#: having worked, on a formatter, or on a handler.
#:
#: Its value is what its *absence* proves. A deploy log that contains this
#: line and nothing else means the factory was entered and died inside;
#: a deploy log without it means the factory was never called at all —
#: which is a platform or start-command problem, not an application one.
#: Distinguishing those two took an entire debugging session once.
_ENTERED = "argus.startup: building service"


def _announce(name: str) -> None:
    print(f"{_ENTERED} name={name}", file=sys.stderr, flush=True)


def _report_startup_failure(name: str, failure: BaseException) -> None:
    """Say loudly why a service could not be built, then let it die.

    Two channels on purpose, because they fail independently:

    1. A structured record, so the failure is queryable alongside every
       other ARGUS log and carries `event=service_startup_failed`.
    2. A plain traceback on stderr, flushed, because the structured path
       depends on `configure_logging` having succeeded — and if *that* is
       what broke, the structured record is exactly what will not appear.

    Never swallows. The caller re-raises so the process still exits
    non-zero and the deploy is still abandoned; the only thing this adds
    is that somebody can tell what happened.
    """
    # Suppressed on purpose: if `configure_logging` is what broke, the
    # structured record is exactly what cannot be written, and the stderr
    # fallback below is the only channel left. Losing the log line must
    # never cost the traceback.
    with contextlib.suppress(Exception):
        _log.exception(
            "service failed to start",
            extra={
                "event": "service_startup_failed",
                "service": name,
                "error_type": type(failure).__name__,
                "error": str(failure),
            },
        )

    print(
        f"argus.startup: FAILED to build service name={name} "
        f"error_type={type(failure).__name__} error={failure}",
        file=sys.stderr,
        flush=True,
    )
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()


def _module_app(name: str) -> Any:
    """Build one service at import, configuring logging first.

    Logging is configured before anything else so that a
    `ProductionMisconfigured` raised during composition is emitted as a
    structured record rather than a bare traceback — a deploy that fails
    should fail legibly in the platform's log viewer.

    It is also what claims ownership of logging configuration, which is
    what stops an in-process Alembic migration from replacing the
    handler. Module 23 found that the hard way; this is the call site
    that makes the fix apply in production rather than only in its test.

    ## Why the whole body is wrapped

    Uvicorn calls this through `--factory`. If it raises, uvicorn's own
    handling is version-dependent and, under `--workers`, the traceback
    is produced in a child process whose stderr a platform may or may not
    surface. The observable result on a hosted platform can be a
    container that simply stops with nothing in the log — which is
    indistinguishable, from the outside, from a container that was never
    started.

    So this never relies on somebody else printing the exception. It
    announces entry before anything can fail, reports any failure through
    two independent channels, and re-raises so the deploy still fails.
    """
    _announce(name)
    configure_logging()
    try:
        return build_service(name)
    except BaseException as failure:  # noqa: BLE001 - logged, then re-raised
        _report_startup_failure(name, failure)
        raise


def terminal_app() -> Any:
    return _module_app("terminal")


def public_stats_app() -> Any:
    return _module_app("public_stats")


def intelligence_app() -> Any:
    return _module_app("intelligence")


def identity_app() -> Any:
    return _module_app("identity")


def health_app() -> Any:
    return _module_app("health")
