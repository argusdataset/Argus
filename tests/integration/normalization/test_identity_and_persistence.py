"""Identity resolution across ticker changes, and persistence guarantees.

These need a real database: the ticker-history exclusion constraints and
the append-only triggers are database behaviour, and a mock would prove
nothing about either.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from data.canonical_model.exchanges import CanonicalExchange
from data.canonical_model.pit import session_close
from data.normalization.identity import SecurityIdentityResolver, TickerResolutionError
from data.normalization.persistence import CanonicalWriter
from data.normalization.pipeline import normalize_security, persist
from data.provider_adapters.fmp.models import (
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    FetchProvenance,
    FinancialStatement,
)

FETCHED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def provenance(endpoint: str) -> FetchProvenance:
    return FetchProvenance(endpoint=endpoint, url_path=f"/stable/{endpoint}", fetched_at=FETCHED_AT)


def bar(symbol: str, day: date, close: str) -> DailyBar:
    price = Decimal(close)
    return DailyBar(
        provenance=provenance("historical_price_eod_full"),
        symbol=symbol,
        bar_date=day,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1_000,
    )


# --------------------------------------------------------------------------
# Ticker changes
# --------------------------------------------------------------------------


def test_old_and_new_tickers_resolve_to_the_same_identity(connection: Connection):
    """The FB -> META case. Two half-histories would be invisible if wrong."""
    resolver = SecurityIdentityResolver(connection)
    changed_at = datetime(2022, 6, 9, tzinfo=UTC)

    original = resolver.register(
        "FB",
        exchange=CanonicalExchange.NASDAQ,
        valid_from=datetime(2012, 5, 18, tzinfo=UTC),
        name="Meta Platforms",
    )
    same = resolver.record_ticker_change(
        old_symbol="FB",
        new_symbol="META",
        changed_at=changed_at,
        exchange=CanonicalExchange.NASDAQ,
    )

    assert same == original
    # A bar from 2019 filed under FB...
    assert resolver.resolve("FB", datetime(2019, 3, 1, tzinfo=UTC)) == original
    # ...and one from 2024 under META are the same security.
    assert resolver.resolve("META", datetime(2024, 3, 1, tzinfo=UTC)) == original


def test_resolution_is_as_of_a_date_not_just_current(connection: Connection):
    resolver = SecurityIdentityResolver(connection)
    resolver.register(
        "FB", exchange=CanonicalExchange.NASDAQ, valid_from=datetime(2012, 5, 18, tzinfo=UTC)
    )
    resolver.record_ticker_change(
        old_symbol="FB",
        new_symbol="META",
        changed_at=datetime(2022, 6, 9, tzinfo=UTC),
        exchange=CanonicalExchange.NASDAQ,
    )

    # META did not exist as a ticker in 2019.
    with pytest.raises(TickerResolutionError):
        resolver.resolve("META", datetime(2019, 3, 1, tzinfo=UTC))


def test_a_recycled_ticker_resolves_to_the_holder_at_that_time(connection: Connection):
    """Two different companies, same ticker, different eras."""
    resolver = SecurityIdentityResolver(connection)

    first = resolver.register(
        "ZZZQ",
        exchange=CanonicalExchange.NYSE,
        valid_from=datetime(2000, 1, 1, tzinfo=UTC),
        valid_to=datetime(2010, 1, 1, tzinfo=UTC),
        name="Original Holder",
    )
    second = resolver.register(
        "ZZZQ",
        exchange=CanonicalExchange.NASDAQ,
        valid_from=datetime(2015, 1, 1, tzinfo=UTC),
        name="New Holder",
    )

    assert first != second
    assert resolver.resolve("ZZZQ", datetime(2005, 1, 1, tzinfo=UTC)) == first
    assert resolver.resolve("ZZZQ", datetime(2020, 1, 1, tzinfo=UTC)) == second


def test_overlapping_ticker_windows_are_rejected_by_the_database(connection: Connection):
    """Migration 0002's exclusion constraint, exercised through this module."""
    resolver = SecurityIdentityResolver(connection)
    resolver.register(
        "OVLP",
        exchange=CanonicalExchange.NYSE,
        valid_from=datetime(2000, 1, 1, tzinfo=UTC),
        valid_to=datetime(2010, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(IntegrityError):
        resolver.register(
            "OVLP",
            exchange=CanonicalExchange.NASDAQ,
            valid_from=datetime(2005, 1, 1, tzinfo=UTC),
        )


def test_unknown_ticker_raises_rather_than_returning_none(connection: Connection):
    resolver = SecurityIdentityResolver(connection)

    with pytest.raises(TickerResolutionError, match="NOSUCH"):
        resolver.resolve("NOSUCH")


def test_bars_under_both_tickers_persist_against_one_identity(connection: Connection):
    """End to end: the two halves of a renamed security's history join up."""
    resolver = SecurityIdentityResolver(connection)
    security_id = resolver.register(
        "FB", exchange=CanonicalExchange.NASDAQ, valid_from=datetime(2012, 5, 18, tzinfo=UTC)
    )
    resolver.record_ticker_change(
        old_symbol="FB",
        new_symbol="META",
        changed_at=datetime(2022, 6, 9, tzinfo=UTC),
        exchange=CanonicalExchange.NASDAQ,
    )
    writer = CanonicalWriter(connection)

    for symbol, day in (("FB", date(2019, 3, 1)), ("META", date(2024, 3, 1))):
        resolved = resolver.resolve(symbol, session_close(day))
        outcome = normalize_security(security_id=resolved, bars=[bar(symbol, day, "100")])
        persist(outcome, writer)

    stored = connection.execute(
        text("SELECT count(*) FROM canonical_ohlcv WHERE security_id = :sid"),
        {"sid": security_id},
    ).scalar_one()
    assert stored == 2


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def _seed_security(connection: Connection, symbol: str = "AAPL"):
    resolver = SecurityIdentityResolver(connection)
    return resolver.register(
        symbol, exchange=CanonicalExchange.NASDAQ, valid_from=datetime(1980, 12, 12, tzinfo=UTC)
    )


def test_bars_persist_with_all_four_pit_columns(connection: Connection):
    security_id = _seed_security(connection)
    writer = CanonicalWriter(connection)

    outcome = normalize_security(
        security_id=security_id, bars=[bar("AAPL", date(2024, 1, 3), "184.25")]
    )
    persist(outcome, writer)

    row = connection.execute(
        text(
            "SELECT event_time, observation_time, availability_time, ingestion_time, "
            "close_raw, close_adjusted FROM canonical_ohlcv WHERE security_id = :sid"
        ),
        {"sid": security_id},
    ).one()

    assert row.event_time == session_close(date(2024, 1, 3))
    assert row.observation_time == row.event_time
    assert row.availability_time > row.observation_time
    assert row.ingestion_time == FETCHED_AT
    assert row.close_raw == Decimal("184.250000")
    assert row.close_adjusted == Decimal("184.250000")


def test_reingestion_is_idempotent(connection: Connection):
    """Resuming an interrupted backfill must not fail or duplicate."""
    security_id = _seed_security(connection)
    writer = CanonicalWriter(connection)
    bars = [bar("AAPL", date(2024, 1, 3), "184.25")]

    first = persist(normalize_security(security_id=security_id, bars=bars), writer)
    second = persist(normalize_security(security_id=security_id, bars=bars), writer)

    assert first.writes["bars"].inserted == 1
    assert second.writes["bars"].inserted == 0
    assert second.writes["bars"].skipped == 1


def test_fundamentals_persist_with_accepted_date_as_observation_time(connection: Connection):
    """The leakage boundary, verified all the way to the database column."""
    security_id = _seed_security(connection)
    writer = CanonicalWriter(connection)

    statement = FinancialStatement(
        provenance=provenance("income_statement"),
        symbol="AAPL",
        statement_type="INCOME_STATEMENT",
        fiscal_date=date(2024, 3, 31),
        period="Q1",
        accepted_date=datetime(2024, 5, 15, 18, 8, 27, tzinfo=UTC),
        data={"revenue": 90753000000},
    )
    persist(normalize_security(security_id=security_id, statements=[statement]), writer)

    row = connection.execute(
        text(
            "SELECT event_time, observation_time, fiscal_period_end, data "
            "FROM canonical_fundamentals WHERE security_id = :sid"
        ),
        {"sid": security_id},
    ).one()

    # Module 03 types fiscal_period_end as DateTime rather than Date, so a
    # date round-trips as midnight UTC. Lossless, but it means reads compare
    # against .date() — noted in the module README.
    assert row.fiscal_period_end.date() == date(2024, 3, 31)
    assert row.event_time.date() == date(2024, 3, 31)
    # Six weeks later, not the period end.
    assert row.observation_time == datetime(2024, 5, 15, 18, 8, 27, tzinfo=UTC)
    assert row.data["revenue"] == 90753000000


def test_a_restatement_is_a_new_row_not_an_edit(connection: Connection):
    """Both observations survive, which is what makes the record checkable."""
    security_id = _seed_security(connection)
    writer = CanonicalWriter(connection)

    def statement(accepted: datetime, revenue: int) -> FinancialStatement:
        return FinancialStatement(
            provenance=provenance("income_statement"),
            symbol="AAPL",
            statement_type="INCOME_STATEMENT",
            fiscal_date=date(2024, 3, 31),
            period="Q1",
            accepted_date=accepted,
            data={"revenue": revenue},
        )

    persist(
        normalize_security(
            security_id=security_id,
            statements=[statement(datetime(2024, 5, 15, tzinfo=UTC), 90_000)],
        ),
        writer,
    )
    persist(
        normalize_security(
            security_id=security_id,
            statements=[statement(datetime(2024, 8, 1, tzinfo=UTC), 91_000)],
        ),
        writer,
    )

    rows = connection.execute(
        text(
            "SELECT observation_time, data FROM canonical_fundamentals "
            "WHERE security_id = :sid ORDER BY observation_time"
        ),
        {"sid": security_id},
    ).all()

    assert len(rows) == 2
    assert rows[0].data["revenue"] == 90_000
    assert rows[1].data["revenue"] == 91_000


def test_corporate_actions_persist_and_drive_the_adjusted_series(connection: Connection):
    security_id = _seed_security(connection)
    writer = CanonicalWriter(connection)

    split = CorporateAction(
        provenance=provenance("splits"),
        symbol="AAPL",
        kind=CorporateActionKind.SPLIT,
        event_date=date(2020, 8, 31),
        details={"numerator": 4, "denominator": 1},
    )
    outcome = normalize_security(
        security_id=security_id,
        bars=[bar("AAPL", date(2020, 8, 28), "400"), bar("AAPL", date(2020, 8, 31), "100")],
        actions=[split],
    )
    persist(outcome, writer)

    rows = connection.execute(
        text(
            "SELECT close_raw, close_adjusted FROM canonical_ohlcv "
            "WHERE security_id = :sid ORDER BY event_time"
        ),
        {"sid": security_id},
    ).all()

    # Raw retained as it printed; adjusted continuous through the split.
    assert [row.close_raw for row in rows] == [Decimal("400.000000"), Decimal("100.000000")]
    assert [row.close_adjusted for row in rows] == [Decimal("100.000000"), Decimal("100.000000")]


# --------------------------------------------------------------------------
# Append-only guarantees, through this module's write path
# --------------------------------------------------------------------------


def test_canonical_bars_cannot_be_updated(connection: Connection):
    """Editing a canonical row would destroy the record of what ARGUS believed."""
    security_id = _seed_security(connection)
    persist(
        normalize_security(security_id=security_id, bars=[bar("AAPL", date(2024, 1, 3), "184.25")]),
        CanonicalWriter(connection),
    )

    with pytest.raises(IntegrityError) as exc_info:
        connection.execute(
            text("UPDATE canonical_ohlcv SET close_raw = 1 WHERE security_id = :sid"),
            {"sid": security_id},
        )
    assert "append-only" in str(exc_info.value)


def test_canonical_bars_cannot_be_deleted(connection: Connection):
    security_id = _seed_security(connection)
    persist(
        normalize_security(security_id=security_id, bars=[bar("AAPL", date(2024, 1, 3), "184.25")]),
        CanonicalWriter(connection),
    )

    with pytest.raises(IntegrityError):
        connection.execute(
            text("DELETE FROM canonical_ohlcv WHERE security_id = :sid"), {"sid": security_id}
        )


def test_canonical_fundamentals_cannot_be_updated(connection: Connection):
    security_id = _seed_security(connection)
    statement = FinancialStatement(
        provenance=provenance("income_statement"),
        symbol="AAPL",
        statement_type="INCOME_STATEMENT",
        fiscal_date=date(2024, 3, 31),
        period="Q1",
        accepted_date=datetime(2024, 5, 15, tzinfo=UTC),
        data={"revenue": 1},
    )
    persist(
        normalize_security(security_id=security_id, statements=[statement]),
        CanonicalWriter(connection),
    )

    with pytest.raises(IntegrityError):
        connection.execute(
            text("UPDATE canonical_fundamentals SET data = '{}'::jsonb WHERE security_id = :sid"),
            {"sid": security_id},
        )


def test_canonical_tables_cannot_be_truncated(connection: Connection):
    """TRUNCATE bypasses row triggers, so it needs its own guard."""
    with pytest.raises(IntegrityError):
        connection.execute(text("TRUNCATE canonical_corporate_actions"))
