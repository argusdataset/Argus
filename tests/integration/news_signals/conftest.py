"""Database fixtures for Module 28.

Everything this module claims is a claim about `canonical_news` rows and
what an aggregate query as of a given instant can see, so — the same
reasoning Modules 03, 09, 12 and 26 already established — this runs
against a real PostgreSQL rather than a mock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from infra.db.schema.identity import universe_membership, universe_version
from infra.db.schema.news import canonical_news
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)

LISTED_FROM = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than DELETE: `canonical_news` is append-only, the same
    reason every other integration suite in this project uses a rolled-
    back transaction rather than a delete-based teardown.
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
            valid_from=LISTED_FROM,
            name=f"{ticker} Test Corp.",
        )

    return _register


@pytest.fixture
def universe(engine: Engine) -> Callable[..., Any]:  # noqa: F811
    """Register securities and put them in a fresh, committed universe version.

    Committed on the real `engine` rather than the rolled-back
    `connection` fixture, and for the same reason Module 26's own
    integration fixtures do this: `run_daily_news_signals` opens its own
    transaction (`engine.begin()`), so a row that only exists inside this
    test's uncommitted `connection` transaction would be invisible to it —
    the orchestrator would see an empty universe regardless of what the
    test set up.
    """

    def _build(tickers: tuple[str, ...]) -> tuple[UUID, dict[str, UUID]]:
        stamp = datetime.now(UTC).timestamp()
        with engine.begin() as conn:
            version_id = conn.execute(
                universe_version.insert()
                .values(
                    version_label=f"module28-{stamp}",
                    as_of_date=LISTED_FROM,
                    definition={"source": "module28 integration test"},
                )
                .returning(universe_version.c.id)
            ).scalar_one()

            resolver = SecurityIdentityResolver(conn)
            identities: dict[str, UUID] = {}
            for ticker in tickers:
                security_id = resolver.register(
                    f"{ticker}{int(stamp * 1000) % 100000}",
                    exchange=CanonicalExchange.NASDAQ,
                    valid_from=LISTED_FROM,
                    name=f"{ticker} Test Corp.",
                )
                identities[ticker] = security_id
                conn.execute(
                    universe_membership.insert().values(
                        universe_version_id=version_id,
                        security_id=security_id,
                        listing_status="LISTED",
                        listed_from=LISTED_FROM,
                        listed_to=None,
                        exchange="NASDAQ",
                        interval_evidence="reported",
                    )
                )
        return version_id, identities

    return _build


@pytest.fixture
def add_article_committed(engine: Engine) -> Callable[..., None]:  # noqa: F811
    """`add_article`'s committed twin, for securities built by `universe`."""

    counter = iter(range(1_000_000))

    def _add(security_id: UUID, *, published: datetime, available: datetime | None = None) -> None:
        available = available or published
        with engine.begin() as conn:
            conn.execute(
                canonical_news.insert().values(
                    security_id=security_id,
                    event_time=published,
                    observation_time=published,
                    availability_time=available,
                    ingestion_time=available,
                    headline=f"{security_id} reports something #{next(counter)}",
                    source_site="example.test",
                    url=None,
                    summary=None,
                    lineage={"source": "module28 integration test"},
                )
            )

    return _add


@pytest.fixture
def add_article(connection: Connection) -> Callable[..., None]:
    """Write one `canonical_news` row with explicit PIT timestamps.

    Deliberately raw rather than going through Module 05's
    `translate_news`: these tests need to place articles at specific past
    instants relative to a scan date, and a helper that derived timestamps
    itself would be testing the deriver instead of the aggregate query.
    """

    counter = iter(range(1_000_000))

    def _add(
        security_id: UUID,
        *,
        published: datetime,
        available: datetime | None = None,
        headline: str | None = None,
        url: str | None = None,
    ) -> None:
        available = available or published
        # Identity is (security_id, headline, event_time) when there is no
        # URL — a test writing several articles for one security on the
        # same day needs distinct headlines or it collides with itself,
        # never mind the security it is describing.
        default_headline = f"{security_id} reports something #{next(counter)}"
        connection.execute(
            canonical_news.insert().values(
                security_id=security_id,
                event_time=published,
                observation_time=published,
                availability_time=available,
                ingestion_time=available,
                headline=headline or default_headline,
                source_site="example.test",
                url=url,
                summary=None,
                lineage={"source": "module28 integration test"},
            )
        )

    return _add


@pytest.fixture
def news_signal_row(connection: Connection) -> Callable[[UUID], Any]:
    """The single stored `news_volume_signals` row for one security, if any."""
    from sqlalchemy import select

    from infra.db.schema.news_signals import news_volume_signals

    def _row(security_id: UUID) -> Any:
        return connection.execute(
            select(news_volume_signals).where(news_volume_signals.c.security_id == security_id)
        ).one_or_none()

    return _row
