"""Fixtures for universe integration tests.

Combines the Module 03 database fixtures (throwaway database migrated to
head, skipped when no PostgreSQL is reachable) with Module 04's
MockTransport fixtures, so construction runs end to end — real fetch code
path, real identity resolution, real database — without a live API call.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)
from tests.unit.fmp.conftest import (  # noqa: F401
    make_client,
    make_fetcher,
)


@pytest.fixture
def config(tmp_path, monkeypatch):
    """FMP config with the response cache disabled.

    These tests vary the listing payload between fetches inside a single
    test to check how construction reacts. The adapter's cache is keyed on
    the request rather than the response, so a cached stock-list would
    serve the first payload back and those assertions would silently be
    testing nothing.
    """
    for key, value in {
        "ARGUS_DATABASE__PORT": "5432",
        "ARGUS_DATABASE__NAME": "argus_test",
        "ARGUS_DATABASE__USER": "argus_test",
        "ARGUS_PROVIDERS__FMP_BASE_URL": "https://fmp.test",
        "ARGUS_PROVIDERS__FMP_CACHE_ENABLED": "false",
        "ARGUS_PROVIDERS__FMP_CACHE_DIR": str(tmp_path / "cache"),
        "ARGUS_PROVIDERS__FMP_CHECKPOINT_DIR": str(tmp_path / "checkpoints"),
        "ARGUS_PROVIDERS__FMP_REQUESTS_PER_MINUTE": "6000",
        "ARGUS_PROVIDERS__FMP_BULK_REQUESTS_PER_MINUTE": "600",
        "ARGUS_PROVIDERS__FMP_BACKOFF_BASE_SECONDS": "0.001",
        "ARGUS_PROVIDERS__FMP_BACKOFF_MAX_SECONDS": "0.01",
    }.items():
        monkeypatch.setenv(key, value)

    from packages.config.settings import AppConfig

    return AppConfig()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than cleanup because the append-only guards on
    `universe_version` and `universe_membership` would refuse a DELETE.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()
