"""Fixtures for Module 19.

The Terminal is a thin layer over eighteen modules of storage, so almost
nothing here is worth testing without a real PostgreSQL: the PIT filters
are SQL, the ticker-history exclusion constraints are SQL, and the
watchlist ownership isolation is a `WHERE` clause. A mocked connection
would test my beliefs about those, not the clauses.

The HTTP client is FastAPI's `TestClient`, which speaks to the app
in-process over httpx — no port, no server, no event loop to manage.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from data.canonical_model.exchanges import CanonicalExchange
from data.canonical_model.records import CanonicalStatementType
from data.normalization.identity import SecurityIdentityResolver
from infra.db.schema.canonical import canonical_fundamentals
from infra.db.schema.news import canonical_news
from services.terminal.app import create_app
from services.terminal.config import TerminalConfig
from services.terminal.identity import USER_HEADER
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)
from tests.integration.feature_engine.conftest import insert_bars

#: The Terminal's "now" in these tests. Fixed rather than wall-clock, so a
#: PIT assertion means the same thing on every run.
NOW = datetime(2026, 3, 3, 21, 0, tzinfo=UTC)
HISTORY_START = datetime(2025, 1, 2, tzinfo=UTC)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than DELETE: the canonical tables are append-only, so
    a delete-based teardown would be refused by Module 03's guards.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def client(connection: Connection) -> Iterator[TestClient]:
    """An HTTP client whose requests run inside this test's transaction.

    `create_app` normally opens a transaction per request from an engine.
    Here the dependency is overridden to hand back the test's own
    connection inside a savepoint, so a request's writes are visible to
    the next request and every one of them is rolled back at the end.
    Without this a test would either commit into the shared database or
    see none of its own writes.
    """
    from services.terminal.app import get_connection

    app = create_app(engine=None, config=TerminalConfig())  # type: ignore[arg-type]

    def _connection() -> Iterator[Connection]:
        savepoint = connection.begin_nested()
        try:
            yield connection
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise

    app.dependency_overrides[get_connection] = _connection
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def register(connection: Connection) -> Callable[..., UUID]:
    """Register a security by ticker and hand back its identity."""
    resolver = SecurityIdentityResolver(connection)

    def _register(
        ticker: str,
        *,
        name: str | None = None,
        valid_from: datetime = datetime(2000, 1, 1, tzinfo=UTC),
    ) -> UUID:
        return resolver.register(
            ticker,
            exchange=CanonicalExchange.NASDAQ,
            valid_from=valid_from,
            name=name or f"{ticker} Test Corp.",
        )

    return _register


@pytest.fixture
def make_user(connection: Connection) -> Callable[[str], UUID]:
    """A real `users` row.

    Real rather than a bare UUID because the identity stub resolves
    against `users` and watchlist ownership is foreign-keyed — see
    `services/terminal/identity.py` on why the stub refuses an id that
    names nothing.
    """
    role_id = connection.execute(
        text("INSERT INTO roles (name) VALUES (:name) RETURNING id"),
        {"name": f"module19-{uuid4()}"},
    ).scalar_one()

    def _make(label: str) -> UUID:
        return connection.execute(
            text(
                "INSERT INTO users (email, display_name, role_id) "
                "VALUES (:email, :name, :role) RETURNING id"
            ),
            {"email": f"{label}-{uuid4()}@example.test", "name": label, "role": role_id},
        ).scalar_one()

    return _make


@pytest.fixture
def as_user() -> Callable[[UUID], dict[str, str]]:
    """Headers identifying a request's user, via the Module 22 stub."""

    def _headers(user_id: UUID) -> dict[str, str]:
        return {USER_HEADER: str(user_id)}

    return _headers


@pytest.fixture
def add_fundamentals(connection: Connection) -> Callable[..., None]:
    """Insert one statement, with explicit PIT timestamps.

    `availability_time` is a separate argument from `fiscal_period_end`
    precisely so a test can express "this quarter ended in March and
    ARGUS could not see it until May", which is the only way to check
    that a cutoff is doing anything.
    """

    def _add(
        security_id: UUID,
        *,
        statement_type: CanonicalStatementType,
        fiscal_period: str,
        fiscal_period_end: datetime,
        available_at: datetime,
        data: dict[str, object],
    ) -> None:
        connection.execute(
            canonical_fundamentals.insert().values(
                security_id=security_id,
                statement_type=statement_type.value,
                fiscal_period=fiscal_period,
                fiscal_period_end=fiscal_period_end,
                event_time=fiscal_period_end,
                observation_time=available_at,
                availability_time=available_at,
                ingestion_time=available_at,
                data=data,
            )
        )

    return _add


@pytest.fixture
def add_news(connection: Connection) -> Callable[..., None]:
    def _add(
        security_id: UUID,
        *,
        headline: str,
        published_at: datetime,
        available_at: datetime | None = None,
        url: str | None = None,
        source_site: str | None = "example.test",
        summary: str | None = None,
    ) -> None:
        available = available_at or published_at
        connection.execute(
            canonical_news.insert().values(
                security_id=security_id,
                event_time=published_at,
                observation_time=published_at,
                availability_time=available,
                ingestion_time=available,
                headline=headline,
                source_site=source_site,
                url=url,
                summary=summary,
                lineage={"provider": "test"},
            )
        )

    return _add


@pytest.fixture
def add_bars(connection: Connection) -> Callable[..., None]:
    """A run of daily bars, using Module 08's own test helper."""

    def _add(
        security_id: UUID,
        *,
        closes: list[float],
        start: datetime = HISTORY_START,
    ) -> None:
        insert_bars(connection, security_id, start=start, closes=closes)

    return _add


@pytest.fixture
def add_split(connection: Connection) -> Callable[..., None]:
    """A 2-for-1 split, so adjustment can be observed rather than assumed."""
    from infra.db.schema.canonical import canonical_corporate_actions

    def _add(
        security_id: UUID,
        *,
        effective: datetime,
        ratio: str = "2",
        available_at: datetime | None = None,
    ) -> None:
        available = available_at or effective
        connection.execute(
            canonical_corporate_actions.insert().values(
                security_id=security_id,
                action_type="SPLIT",
                effective_date=effective,
                event_time=effective,
                observation_time=available,
                availability_time=available,
                ingestion_time=available,
                details={"numerator": ratio, "denominator": "1"},
            )
        )

    return _add


__all__ = ["HISTORY_START", "NOW", "Decimal", "timedelta"]
