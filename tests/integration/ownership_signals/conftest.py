"""Database fixtures for Module 29.

Everything worth checking here is a claim about what a query at a given
`as_of` can see — an insider purchase still inside its two-day disclosure
window, a quarter whose 13F filing deadline has not passed. A mocked
connection would return whatever the leakage tests expected it to, so
these run against a real PostgreSQL, the same reasoning Modules 03, 09,
12, 26 and 28 already established.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.ownership_signals.config import OwnershipThresholds
from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from infra.db.schema.identity import universe_membership, universe_version
from infra.db.schema.ownership_signals import insider_trades, institutional_ownership
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)

LISTED_FROM = datetime(2020, 1, 1, tzinfo=UTC)


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
            valid_from=LISTED_FROM,
            name=f"{ticker} Test Corp.",
        )

    return _register


@pytest.fixture
def add_trade(connection: Connection) -> Callable[..., None]:
    """Write one `insider_trades` row with explicit PIT timestamps.

    Deliberately raw rather than going through `translate_insider_transaction`:
    the leakage tests need `availability_time` at a specific instant, and a
    helper that derived it would be testing the deriver instead of the
    query.
    """
    thresholds = OwnershipThresholds()

    def _add(
        security_id: UUID,
        *,
        traded_at: datetime,
        person: str,
        code: str = "P",
        quantity: Decimal | int = 1000,
        available: datetime | None = None,
    ) -> None:
        knowable = available or traded_at + thresholds.insider_availability_lag
        connection.execute(
            insider_trades.insert().values(
                security_id=security_id,
                event_time=traded_at,
                observation_time=knowable,
                availability_time=knowable,
                ingestion_time=knowable,
                reporting_person=person,
                reporting_position="officer: CFO",
                transaction_code=code,
                quantity=Decimal(str(quantity)),
                price=Decimal("10.00"),
                lineage={"source": "module29 integration test"},
                data={"source": "module29 integration test"},
            )
        )

    return _add


@pytest.fixture
def add_quarter(connection: Connection) -> Callable[..., None]:
    """Write one `institutional_ownership` row for a given quarter."""
    from core.ownership_signals.institutional import content_fingerprint

    thresholds = OwnershipThresholds()

    def _add(
        security_id: UUID,
        *,
        year: int,
        quarter: int,
        investors: int | None = None,
        shares: int | None = None,
        percent: float | None = None,
        available: datetime | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        from core.ownership_signals.institutional import quarter_end

        ends = datetime.combine(
            quarter_end(_Quarter(year, quarter)), datetime.max.time(), tzinfo=UTC
        )
        knowable = available or ends + thresholds.institutional_availability_lag
        data = dict(payload or {})
        if investors is not None:
            data.setdefault("investorsHolding", investors)
        if shares is not None:
            data.setdefault("numberOf13Fshares", shares)
        if percent is not None:
            data.setdefault("ownershipPercent", percent)

        connection.execute(
            institutional_ownership.insert().values(
                security_id=security_id,
                year=year,
                quarter=quarter,
                # The real fingerprint, from the module that computes it
                # — not a stub. Two fixture rows that differ in their
                # figures must differ here too, or a test that means to
                # write two observations of a quarter silently writes one.
                content_fingerprint=content_fingerprint({"year": year, "quarter": quarter, **data}),
                event_time=ends,
                observation_time=knowable,
                availability_time=knowable,
                ingestion_time=knowable,
                lineage={"source": "module29 integration test"},
                data=data,
            )
        )

    return _add


class _Quarter:
    """Minimal stand-in for `OwnershipQuarter`, to keep `quarter_end`
    usable from the fixture without importing the dataclass into every
    test module's namespace."""

    __slots__ = ("year", "quarter")

    def __init__(self, year: int, quarter: int) -> None:
        self.year = year
        self.quarter = quarter


@pytest.fixture
def universe(engine: Engine) -> Callable[..., Any]:  # noqa: F811
    """Register securities into a fresh, committed universe version.

    Committed rather than rolled back, for the reason Module 28's own
    fixtures give: `run_daily_ownership_signals` opens its own
    transaction, so a row visible only inside this test's uncommitted one
    would make the orchestrator see an empty universe.
    """

    def _build(tickers: tuple[str, ...]) -> tuple[UUID, dict[str, UUID]]:
        stamp = datetime.now(UTC).timestamp()
        with engine.begin() as conn:
            version_id = conn.execute(
                universe_version.insert()
                .values(
                    version_label=f"module29-{stamp}",
                    as_of_date=LISTED_FROM,
                    definition={"source": "module29 integration test"},
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
def days() -> Callable[[int], timedelta]:
    return lambda count: timedelta(days=count)
