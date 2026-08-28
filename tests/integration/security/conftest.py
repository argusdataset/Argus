"""Fixtures for Module 24.

Reuses Module 22's identity fixtures directly rather than rebuilding
them — `signed_up`, `logged_in`, `GOOD_PASSWORD` and the rest describe
real accounts through the real API, and this module's tests are checking
what happens *around* those requests (headers, rate limits, roles), not
reinventing how an account gets made.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection

from infra.security.config import SecurityConfig, SecuritySettings
from services.identity.app import create_app as create_identity
from services.identity.app import get_connection as identity_connection
from services.identity.config import IdentityConfig
from services.intelligence.app import create_app as create_intelligence
from services.intelligence.app import get_connection as intelligence_connection
from services.public_stats.app import create_app as create_public_stats
from services.public_stats.app import get_connection as public_stats_connection
from services.terminal.app import create_app as create_terminal
from services.terminal.app import get_connection as terminal_connection
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)
from tests.integration.identity.conftest import (  # noqa: F401
    GOOD_PASSWORD,
    connection,
    email,
    enrol_mfa,
    logged_in,
    promote,
    register,
    signed_up,
    stored_hash,
)


@pytest.fixture
def app_client(connection: Connection):  # noqa: F811
    """A factory building any of the four services under a chosen `SecurityConfig`.

    Every hardening test in this file cares about one thing: what the
    middleware stack does to a request, not which service handles it —
    so one factory serves all of them rather than four near-identical
    fixtures.
    """

    builders = {
        "identity": (create_identity, identity_connection),
        "terminal": (create_terminal, terminal_connection),
        "intelligence": (create_intelligence, intelligence_connection),
        "public_stats": (create_public_stats, public_stats_connection),
    }

    def _client(create: str = "identity", *, security: SecurityConfig | None = None) -> TestClient:
        factory, get_connection_dep = builders[create]
        app = factory(engine=None, security=security)  # type: ignore[arg-type]

        def _connection() -> Iterator[Connection]:
            savepoint = connection.begin_nested()
            try:
                yield connection
                savepoint.commit()
            except Exception:
                savepoint.rollback()
                raise

        app.dependency_overrides[get_connection_dep] = _connection
        return TestClient(app, raise_server_exceptions=False)

    return _client


@pytest.fixture
def client(app_client) -> TestClient:
    """The identity app, default security config — most tests want this."""
    return app_client("identity")


@pytest.fixture
def tight_rate_limit() -> SecurityConfig:
    """A ceiling low enough to trip in a handful of requests, for testing."""
    return SecurityConfig(
        settings=SecuritySettings(
            request_rate_limit=type(SecuritySettings().request_rate_limit)(
                value=5.0, kind="security", rationale="test"
            ),
            request_rate_window_seconds=type(SecuritySettings().request_rate_window_seconds)(
                value=60.0, kind="security", rationale="test"
            ),
            rate_limit_block_seconds=type(SecuritySettings().rate_limit_block_seconds)(
                value=2.0, kind="security", rationale="test"
            ),
        )
    )


@pytest.fixture
def config() -> IdentityConfig:
    return IdentityConfig()
