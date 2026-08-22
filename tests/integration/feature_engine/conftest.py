"""Fixtures for the claims that are about the database, not the arithmetic.

Three things in Module 08 cannot be tested without a real PostgreSQL,
because they are assertions about what SQL returns rather than about what
numpy computes:

- **PIT correctness.** "A corporate action ingested later does not touch
  an earlier `as_of`" is a claim about a `WHERE availability_time <=
  :as_of` clause. Mocking the loader would test my belief about the
  clause, not the clause.
- **MissReason propagation.** A recently-listed security has genuinely
  few rows; that has to come from the database to be the real case.
- **Query counting.** The batch path's central property is *two queries
  regardless of universe size*, which only means something against a real
  driver.

Reuses Module 03's database fixtures — throwaway database migrated to
head, skipped cleanly when no server is reachable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from data.canonical_model.exchanges import CanonicalExchange
from data.canonical_model.pit import session_close
from data.canonical_model.records import (
    CanonicalCorporateActionType,
    CanonicalTimeframe,
)
from data.normalization.identity import SecurityIdentityResolver
from infra.db.schema.canonical import canonical_corporate_actions, canonical_ohlcv
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than cleanup: `canonical_ohlcv` is append-only, so a
    DELETE-based teardown would be refused by the Module 03 guard.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def register(connection: Connection) -> Callable[[str], UUID]:
    """Register a security by ticker and hand back its ID."""
    resolver = SecurityIdentityResolver(connection)

    def _register(ticker: str) -> UUID:
        return resolver.register(
            ticker,
            exchange=CanonicalExchange.NASDAQ,
            valid_from=datetime(2000, 1, 1, tzinfo=UTC),
            name=f"{ticker} Test Corp.",
        )

    return _register


def insert_bars(
    connection: Connection,
    security_id: UUID,
    *,
    start: datetime,
    closes: list[float],
    volume: int = 1_000_000,
    availability_lag: timedelta = timedelta(hours=1),
) -> list[datetime]:
    """Write a run of consecutive business-day bars.

    `availability_lag` is deliberately non-zero: a bar is knowable shortly
    *after* its session closes, never at the instant of the close, and
    tests that assume otherwise would pass against an implementation that
    had the boundary wrong by a day.
    """
    dates = pd.bdate_range(start, periods=len(closes), tz="UTC")
    event_times: list[datetime] = []

    for date, close in zip(dates, closes, strict=True):
        event_time = session_close(date.date())
        available = event_time + availability_lag
        price = Decimal(str(round(close, 4)))
        connection.execute(
            canonical_ohlcv.insert().values(
                security_id=security_id,
                timeframe=CanonicalTimeframe.DAILY.value,
                event_time=event_time,
                observation_time=available,
                availability_time=available,
                ingestion_time=available,
                open_raw=price,
                high_raw=price * Decimal("1.01"),
                low_raw=price * Decimal("0.99"),
                close_raw=price,
                volume_raw=volume,
            )
        )
        event_times.append(event_time)

    return event_times


def insert_split(
    connection: Connection,
    security_id: UUID,
    *,
    effective_date: datetime,
    availability_time: datetime,
    numerator: int,
    denominator: int,
) -> None:
    """Write one split, with its own availability time.

    Splitting `effective_date` from `availability_time` is the entire
    point of the PIT-leakage fixture: a provider commonly backfills a
    corporate action days or weeks after it took effect, and ARGUS must
    reflect what it knew, not what turned out to be true.
    """
    connection.execute(
        canonical_corporate_actions.insert().values(
            security_id=security_id,
            action_type=CanonicalCorporateActionType.SPLIT.value,
            effective_date=effective_date,
            event_time=effective_date,
            observation_time=availability_time,
            availability_time=availability_time,
            ingestion_time=availability_time,
            details={"numerator": numerator, "denominator": denominator},
        )
    )
