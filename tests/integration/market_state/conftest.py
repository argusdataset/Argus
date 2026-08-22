"""Database fixtures for Module 10.

Transition recording, the state projection, and the watchlist queries are
all claims about what the database holds, so they run against a real
PostgreSQL rather than a mock — the same reasoning Modules 03 and 07
established.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
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

    Rollback rather than cleanup: `market_state_transitions` is
    append-only, so a DELETE-based teardown would be refused by the
    Module 03 guard.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def register(connection: Connection) -> Callable[[str], UUID]:
    resolver = SecurityIdentityResolver(connection)

    def _register(ticker: str) -> UUID:
        return resolver.register(
            ticker,
            exchange=CanonicalExchange.NASDAQ,
            valid_from=datetime(2000, 1, 1, tzinfo=UTC),
            name=f"{ticker} Test Corp.",
        )

    return _register
