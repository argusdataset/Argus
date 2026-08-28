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
