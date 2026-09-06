"""Every deployed service, composed exactly as `asgi.py` composes it.

The bug this file exists for was real and would only have appeared in
production: the generated Railway config points every web service's
health check at `/health/live`, and only Module 24's health service has
that route. The other four would have answered 404, and Railway would
have refused to route traffic to any of them — after a green test suite
and a successful build.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from infra.deploy.asgi import SERVICES, build_service
from infra.deploy.config import PROFILES, profile_for
from infra.deploy.liveness import LIVENESS_PATH
from infra.deploy.migrate import upgrade_to_head
from infra.deploy.processes import PROCESSES
from infra.deploy.public_web import FRONTEND_CSP
from packages.config.environment import Environment

WEB = sorted(name for name, process in PROCESSES.items() if process.is_web)


@pytest.fixture
def deployed(fresh_engine: Engine, alembic_target, monkeypatch) -> Iterator[Engine]:
    """A migrated database that `build_service` will build its own engine from."""
    upgrade_to_head(fresh_engine)

    url = alembic_target
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    monkeypatch.setenv("ARGUS_ENV", "development")
    monkeypatch.setenv("ARGUS_DATABASE__HOST", url.host or "localhost")
    monkeypatch.setenv("ARGUS_DATABASE__PORT", str(url.port or 5432))
    monkeypatch.setenv("ARGUS_DATABASE__NAME", url.database or "")
    monkeypatch.setenv("ARGUS_DATABASE__USER", url.username or "")

    from packages.config.settings import get_config

    get_config.cache_clear()
    yield fresh_engine
    get_config.cache_clear()


def test_the_service_registry_matches_the_process_definitions(deployed: Engine):
    """A service with no process never deploys; a process with no service
    deploys a start command that cannot import."""
    assert set(SERVICES) == set(WEB)


@pytest.mark.parametrize("name", WEB)
def test_every_deployed_service_answers_the_platform_health_check(deployed: Engine, name: str):
    """Without this, Railway never routes traffic to four of the five."""
    with TestClient(build_service(name)) as client:
        response = client.get(LIVENESS_PATH)

    assert response.status_code == 200
    assert response.json() == {"status": "up"}


@pytest.mark.parametrize("name", WEB)
def test_the_health_path_a_service_answers_is_the_one_railway_polls(deployed: Engine, name: str):
    assert PROCESSES[name].health_path == LIVENESS_PATH


@pytest.mark.parametrize("name", WEB)
def test_every_deployed_service_carries_module_24s_security_headers(deployed: Engine, name: str):
    """`harden()` is inside the composition, unmodified. This proves it is
    still there after two middlewares were wrapped around it."""
    with TestClient(build_service(name)) as client:
        response = client.get(LIVENESS_PATH)

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("name", WEB)
def test_a_service_built_for_production_sends_hsts(deployed: Engine, name: str):
    """The composition order matters: TLS policy is outermost, so its
    header survives everything the service does below it."""
    profile = PROFILES[Environment.PRODUCTION]
    with TestClient(
        build_service(name, profile=profile), base_url="https://api.argus.test"
    ) as client:
        response = client.get("/thing-that-does-not-exist")

    assert "strict-transport-security" in response.headers


@pytest.mark.parametrize("name", WEB)
def test_a_service_refuses_to_build_under_a_misconfigured_profile(
    deployed: Engine, name: str, monkeypatch
):
    """Startup is the last moment a wildcard CORS origin can be caught.

    After this the process is serving, and the only remaining signal is
    a browser doing something nobody intended.
    """
    monkeypatch.setenv("ARGUS_CORS_ORIGINS", "*")
    from infra.deploy.config import ProductionMisconfigured

    with pytest.raises(ProductionMisconfigured):
        build_service(
            name, profile=profile_for(env={"ARGUS_ENV": "production", "ARGUS_CORS_ORIGINS": "*"})
        )


# --------------------------------------------------------------------------
# ARGUS Public's frontend — mounted onto public_stats only
#
# The bug this section exists for was also real, and also would only
# have appeared in a browser: Module 24's `SecurityHeadersMiddleware`
# sets `Content-Security-Policy: default-src 'none'`, correctly, for a
# JSON API. Mounting a page with its own `<script src>` and `<link
# rel="stylesheet">` under that policy serves a blank page — the HTML
# loads, and the browser silently refuses both requests referenced from
# it. No test that only checks HTTP status codes would have caught it;
# `test_the_frontend_can_actually_load_its_own_assets_under_the_real_csp`
# is written the way that bug was actually found, by checking which
# header value survives the real middleware stack.
# --------------------------------------------------------------------------


def test_only_public_stats_serves_the_frontend(deployed: Engine):
    """The static mount is specific to one service — a deliberate choice.

    A frontend on `terminal` or `identity` would be a second, unrelated
    static site with no reason to exist there; `public_web.py` mounts
    onto `public_stats` alone, and this is the test that would fail if a
    future edit mounted it more broadly than intended.
    """
    with TestClient(build_service("public_stats")) as client:
        assert client.get("/").status_code == 200
        assert "<title>ARGUS" in client.get("/").text

    for name in WEB:
        if name == "public_stats":
            continue
        with TestClient(build_service(name)) as client:
            assert client.get("/").status_code == 404


def test_the_frontend_can_actually_load_its_own_assets_under_the_real_csp(deployed: Engine):
    """Regression test for the bug the section docstring describes.

    Runs through `build_service`, so this is Module 24's real
    `SecurityHeadersMiddleware` — not a stub — deciding whether the
    frontend's CSP override actually wins the `setdefault` race.
    """
    with TestClient(build_service("public_stats")) as client:
        page = client.get("/")
        script = client.get("/app.js")
        style = client.get("/style.css")

    assert page.headers["content-security-policy"] == FRONTEND_CSP
    assert "'self'" in page.headers["content-security-policy"]
    assert script.status_code == 200
    assert style.status_code == 200
    # The failure mode this guards: a browser blocks same-origin script
    # and stylesheet requests entirely under `default-src 'none'`, which
    # a status-code check on /app.js and /style.css alone cannot see —
    # both return 200 to curl regardless of what CSP header rides along.
    # The assertion that matters is on the header the *page* carries,
    # above, since that is what a browser actually enforces against.


def test_the_json_api_keeps_module_24s_strict_csp_unchanged(deployed: Engine):
    """The override is scoped to the static mount, not the whole service.

    `/public/stats` never serves HTML or script and has no reason to
    relax `default-src 'none'` — proving that stays untouched is what
    makes the frontend's own relaxation a scoped exception rather than a
    quiet widening of Module 24's default for the entire service.
    """
    with TestClient(build_service("public_stats")) as client:
        response = client.get("/public/stats")

    assert (
        response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    )


def test_the_frontend_is_reachable_over_the_liveness_and_tls_layers_too(deployed: Engine):
    """The static mount sits inside `create_app()`'s own app object, which
    `build_service` then wraps in liveness and TLS middleware exactly like
    every other service — so it has to survive that wrapping too."""
    profile = PROFILES[Environment.PRODUCTION]
    with TestClient(
        build_service("public_stats", profile=profile), base_url="https://api.argus.test"
    ) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "strict-transport-security" in response.headers


# --------------------------------------------------------------------------
# The `X-Argus-User` stub, and why it is a profile field
#
# Module 19 shipped `stub_identity_enabled=True` so its own suite would
# pass, and Module 22's real-auth seam kept that default deliberately for
# the same reason. What nobody noticed is that `asgi.py`'s factories
# passed no config at all, so every *deployed* Terminal and Intelligence
# took the default and trusted the header — and `services/intelligence`
# constructed a `TerminalConfig()` inline, so it could not be turned off
# from outside at any price.
#
# `curl -H 'X-Argus-User: <uuid>'` was therefore enough to read and
# delete another user's watchlists, the moment a first user existed.
#
# The fix is not a better default. A default is exactly what was
# forgotten, so the config is now *derived* from the deployment profile
# and production has no path that produces a stub-enabled app. These
# tests assert the outcome rather than the mechanism: a production-built
# service refuses the header.
# --------------------------------------------------------------------------


def test_a_production_terminal_refuses_the_identity_header(deployed: Engine, make_user):
    """The vulnerability, asserted from the outside.

    A real user id in `X-Argus-User` — the exact request that used to be
    served as that user — must not reach their watchlists. 501 is the
    right refusal and `errors.py` explains why: the caller reached for a
    mechanism this deployment does not offer, which is not their fault.
    """
    user_id = make_user("victim")
    profile = PROFILES[Environment.PRODUCTION]

    with TestClient(
        build_service("terminal", profile=profile), base_url="https://api.argus.test"
    ) as client:
        response = client.get("/terminal/watchlists", headers={"X-Argus-User": str(user_id)})

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "IDENTITY_UNAVAILABLE"


def test_a_development_terminal_still_accepts_it(deployed: Engine, make_user):
    """The stub is a development affordance and stays one.

    Without this the fix could be "turn it off everywhere", which would
    break Modules 19-21's suites and leave a developer with no way in
    until a session issuer is running locally.
    """
    user_id = make_user("developer")

    with TestClient(build_service("terminal")) as client:
        response = client.get("/terminal/watchlists", headers={"X-Argus-User": str(user_id)})

    assert response.status_code == 200


def test_a_production_intelligence_does_not_trust_the_header_either(deployed: Engine, make_user):
    """The service that hardcoded `TerminalConfig()`.

    Its user is optional — no route there is personalised yet — so the
    observable difference is not a refusal but *who the request is*. The
    config it carries is the assertion available today, and it is the
    thing that was impossible to change before.
    """
    profile = PROFILES[Environment.PRODUCTION]
    app = build_service("intelligence", profile=profile)

    assert _service_config(app).stub_identity_enabled is False


@pytest.mark.parametrize("name", ["terminal", "intelligence"])
def test_no_identity_bearing_service_enables_the_stub_in_production(deployed: Engine, name: str):
    """Both services, one assertion, so a third one added later is noticed."""
    app = build_service(name, profile=PROFILES[Environment.PRODUCTION])

    assert _service_config(app).stub_identity_enabled is False


@pytest.mark.parametrize("name", ["terminal", "intelligence"])
def test_a_hand_built_stub_config_is_refused_under_a_production_profile(
    deployed: Engine, name: str
):
    """The derivation is the fix; this is the guard behind it.

    `_terminal_config` is one edit away from being a hazard again, so
    `check_identity_stub` refuses a stub-enabled config under a profile
    that forbids it — wherever that config came from.
    """
    from infra.deploy.config import ProductionMisconfigured

    with pytest.raises(ProductionMisconfigured, match="X-Argus-User"):
        PROFILES[Environment.PRODUCTION].check_identity_stub(True)


#: The attribute each composition layer keeps its inner app under. Both
#: spellings appear — Starlette's own middlewares use `app`, ARGUS's two
#: pure-ASGI ones use `_app` — so unwrapping tries both rather than
#: assuming a depth, which is `asgi.py`'s to change.
_INNER_APP_ATTRS = ("_app", "app")


def _service_config(app):
    """The config object a composed service is actually running on.

    `build_service` returns the TLS middleware wrapping liveness wrapping
    the FastAPI app, so the config is several layers down. Walks in
    rather than indexing, and fails loudly if it cannot get there — a
    helper that silently returned the wrong object would make every
    assertion below meaningless.
    """
    seen = app
    for _ in range(10):
        if hasattr(seen, "state") and hasattr(seen.state, "config"):
            return seen.state.config
        for attr in _INNER_APP_ATTRS:
            inner = getattr(seen, attr, None)
            if inner is not None and inner is not seen:
                seen = inner
                break
        else:  # pragma: no cover - only reachable if composition changes
            raise AssertionError(f"could not reach the service config from {app!r}")
    raise AssertionError(f"composition nested deeper than expected from {app!r}")
