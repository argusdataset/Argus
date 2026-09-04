"""Shared fixtures for packages/config tests."""

import os

import pytest

from packages.config.settings import get_config

REQUIRED_DB_ENV = {
    "ARGUS_DATABASE__PORT": "5432",
    "ARGUS_DATABASE__NAME": "argus_test",
    "ARGUS_DATABASE__USER": "argus_test_user",
}


@pytest.fixture(autouse=True)
def _isolated_config_env(monkeypatch):
    """Clear ARGUS_* env vars and the get_config cache around every test.

    `DATABASE_URL` is cleared too, and not only for tidiness: since G3 was
    fixed it is a genuine source for the `database` group, so a machine
    that happens to export one would silently satisfy the very fields the
    "missing database group raises" tests exist to check. A test whose
    result depends on the developer's shell is not a test.
    """
    for key in list(os.environ):
        if key.startswith("ARGUS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture
def required_db_env(monkeypatch):
    """Set the minimum env vars AppConfig needs to load successfully."""
    for key, value in REQUIRED_DB_ENV.items():
        monkeypatch.setenv(key, value)
