"""Fixtures for Module 09's database-backed tests.

The unit tests construct feature vectors directly, so a failure there is
unambiguously about a gate's logic. These tests do the opposite: they run
Module 08 over real bars in a real database and feed its actual output
into Module 09, because the coupling between the two modules is itself
something that can break — a renamed feature, a changed evidence field, a
`MissReason` that stops propagating — and no amount of hand-built fixture
would notice.

Reuses Module 03's throwaway-database fixtures and Module 08's bar-writing
helpers rather than reimplementing either.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.universe.intervals import IntervalEvidence
from data.canonical_model.exchanges import CanonicalExchange
from data.canonical_model.records import CanonicalStatementType
from data.normalization.identity import SecurityIdentityResolver
from infra.db.enums import ListingStatus
from infra.db.schema.canonical import canonical_fundamentals
from infra.db.schema.identity import universe_membership, universe_version
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)
from tests.integration.feature_engine.conftest import insert_bars  # noqa: F401


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test."""
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


@pytest.fixture
def universe_version_id(connection: Connection) -> UUID:
    """One universe version to attach listing intervals to."""
    return connection.execute(
        universe_version.insert()
        .values(
            version_label=f"module09-test-{uuid4()}",
            as_of_date=datetime(2024, 6, 3, tzinfo=UTC),
            definition={"source": "module09 integration test"},
        )
        .returning(universe_version.c.id)
    ).scalar_one()


@pytest.fixture
def add_member(connection: Connection, universe_version_id: UUID) -> Callable[..., None]:
    """Give a security a listing interval covering the test dates."""

    def _add(
        security_id: UUID,
        *,
        status: ListingStatus = ListingStatus.LISTED,
        listed_from: datetime = datetime(2015, 1, 1, tzinfo=UTC),
        listed_to: datetime | None = None,
        from_evidence: IntervalEvidence = IntervalEvidence.PRICE_HISTORY,
        to_evidence: IntervalEvidence = IntervalEvidence.PRICE_HISTORY,
    ) -> None:
        connection.execute(
            universe_membership.insert().values(
                universe_version_id=universe_version_id,
                security_id=security_id,
                listing_status=status.value,
                exchange=CanonicalExchange.NASDAQ.value,
                listed_from=listed_from,
                listed_to=listed_to,
                interval_evidence=f"from={from_evidence.value};to={to_evidence.value}",
            )
        )

    return _add


@pytest.fixture
def add_statement(connection: Connection) -> Callable[..., None]:
    """Write one fundamentals statement with an explicit availability time."""

    def _add(
        security_id: UUID,
        statement_type: CanonicalStatementType,
        *,
        period_end: datetime,
        available: datetime,
        data: dict[str, Any],
        fiscal_period: str = "Q1",
    ) -> None:
        connection.execute(
            canonical_fundamentals.insert().values(
                security_id=security_id,
                statement_type=statement_type.value,
                fiscal_period=fiscal_period,
                fiscal_period_end=period_end,
                event_time=period_end,
                observation_time=available,
                availability_time=available,
                ingestion_time=available,
                data=data,
            )
        )

    return _add


# --------------------------------------------------------------------------
# Price shapes
# --------------------------------------------------------------------------

#: Bars of the setup that must sit inside Module 08's loaded window.
#:
#: `load_panel` bounds history to the longest lookback the spec needs
#: (252 bars, ~321 business days of slack). Anything earlier is simply not
#: loaded. A fixture that puts its decline 500 bars before `as_of` therefore
#: presents Module 08 with a flat series, `drawdown_pct` comes back 0, and
#: every security is excluded as "at its highs" — which reads as a Module 09
#: bug and is a fixture bug. Learned the hard way; keeping the whole setup
#: inside 300 bars is what prevents it.
DECLINE_BARS = 120
BASE_BARS = 180


def decline_then_base(
    bars: int,
    *,
    high: float = 40.0,
    low: float = 12.0,
    decline_bars: int = DECLINE_BARS,
    base_bars: int = BASE_BARS,
) -> list[float]:
    """A fall from `high` to `low`, then a long quiet range at `low`.

    Built backwards from the last bar so the decline and the base always
    land inside the loaded window, whatever `bars` is. Everything before
    them is flat filler that Module 08 will not load.
    """
    filler = max(0, bars - decline_bars - base_bars)
    decline = [high - (high - low) * (index / decline_bars) for index in range(decline_bars)]
    base = [low + (index % 9) * 0.03 for index in range(base_bars)]
    return [high] * filler + decline + base


def rising(bars: int, *, start: float = 10.0, end: float = 40.0) -> list[float]:
    """Up throughout — at its highs, so not this setup by definition."""
    return [start + (end - start) * (index / bars) for index in range(bars)]
