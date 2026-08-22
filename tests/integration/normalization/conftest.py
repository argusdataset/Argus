"""Fixtures for normalization integration tests.

Reuses the database fixtures from the Module 03 suite (a throwaway
database migrated to head, skipped when no PostgreSQL is reachable) by
importing them here.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

# Re-exported so pytest resolves `admin_url` / `migrated_database` /
# `engine` for tests in this package.
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection whose transaction is rolled back after each test.

    Keeps tests independent without needing to delete rows — which the
    append-only guards would refuse anyway.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()
