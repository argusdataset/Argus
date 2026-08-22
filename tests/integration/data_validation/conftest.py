"""Fixtures for data-validation integration tests.

Real PostgreSQL: PIT enforcement is a claim about what SQL actually
returns, and a mock would just re-assert whatever the test author already
believed. Reuses the Module 03 database fixtures (throwaway database
migrated to head, skipped cleanly when no server is reachable).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than cleanup: several of the tables written to in
    these tests are append-only, so a DELETE-based cleanup would be
    refused by the same guard the tests are exercising.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def security_id(connection: Connection) -> UUID:
    """One registered security, ready to attach canonical rows to."""
    resolver = SecurityIdentityResolver(connection)
    return resolver.register(
        "AAPL",
        exchange=CanonicalExchange.NASDAQ,
        valid_from=datetime(1980, 12, 12, tzinfo=UTC),
        name="Apple Inc.",
    )
