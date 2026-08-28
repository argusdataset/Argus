"""Booting the way a platform boots: a connection string and nothing else.

This file exists because Module 25 shipped with a suite that passed and a
first deploy that crashed. Every test that touched configuration built an
`AppConfig` and handed it in, and every test that built a service set the
four `ARGUS_DATABASE__*` variables. Nothing ever ran the path a deployed
container runs — resolve your own configuration, from an environment
where a hosting platform has injected `DATABASE_URL` and none of the
discrete fields.

So these tests deliberately *unset* everything a developer's machine has
and a platform does not, and then run the real entrypoints.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import URL

from infra.deploy import asgi, migrate
from infra.deploy.liveness import LIVENESS_PATH
from infra.deploy.processes import PROCESSES
from packages.config.settings import get_config

#: Everything a laptop or CI run has that a Railway container does not.
DEVELOPER_ONLY = (
    "ARGUS_DATABASE__HOST",
    "ARGUS_DATABASE__PORT",
    "ARGUS_DATABASE__NAME",
    "ARGUS_DATABASE__USER",
    "DATABASE_PASSWORD",
)

WEB = sorted(name for name, process in PROCESSES.items() if process.is_web)


@pytest.fixture
def platform(fresh_database: URL, monkeypatch, tmp_path) -> Iterator[URL]:
    """A container's environment: `DATABASE_URL`, `ARGUS_ENV`, `PORT`.

    `dotenv_path` is pointed at a file that does not exist, because a
    developer's `.env` would otherwise supply exactly the values this
    test is trying to prove are unnecessary — and the test would pass on
    a laptop and fail in production, which is the failure mode that made
    this file necessary.
    """
    for key in DEVELOPER_ONLY:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("ARGUS_MIGRATION_DATABASE_URL", raising=False)

    monkeypatch.setenv("ARGUS_SECRETS__DOTENV_PATH", str(tmp_path / "absent.env"))
    monkeypatch.setenv("DATABASE_URL", fresh_database.render_as_string(hide_password=False))
    monkeypatch.setenv("ARGUS_ENV", "production")
    monkeypatch.setenv("PORT", "8000")

    get_config.cache_clear()
    yield fresh_database
    get_config.cache_clear()


def test_the_pre_deploy_migration_runs_with_only_a_connection_string(platform: URL):
    """Step 2 of the deploy. A non-zero exit here abandons the deploy."""
    assert migrate.main([]) == 0
    assert migrate.pending_migrations().up_to_date


@pytest.mark.parametrize("name", WEB)
def test_every_service_boots_with_only_a_connection_string(platform: URL, name: str):
    """Step 3, and the one that actually crashed.

    `uvicorn infra.deploy.asgi:<factory> --factory` calls exactly this.
    """
    migrate.main([])

    factory = getattr(asgi, PROCESSES[name].asgi_factory)
    with TestClient(factory(), base_url="http://argus.up.railway.app") as client:
        response = client.get(LIVENESS_PATH, follow_redirects=False)

    assert response.status_code == 200
    assert response.json() == {"status": "up"}


def test_a_plaintext_request_is_still_redirected_in_production(platform: URL):
    """Proves `ARGUS_ENV=production` was genuinely picked up.

    A service that booted under the development profile by accident would
    pass every test above and quietly serve production without HSTS.
    """
    migrate.main([])

    with TestClient(asgi.terminal_app(), base_url="http://argus.up.railway.app") as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"].startswith("https://")


def test_the_scanner_entrypoint_reaches_its_own_refusal(platform: URL):
    """Not a crash on configuration — a refusal on a missing universe.

    Exit 2 means the process started, connected, and declined for the
    documented reason. Exit 1 would mean something else broke.
    """
    from infra.deploy import scanner

    migrate.main([])
    assert scanner.main([]) == 2


def test_the_retention_entrypoint_runs_with_only_a_connection_string(platform: URL):
    from infra.deploy import retention

    migrate.main([])
    assert retention.main([]) == 0
