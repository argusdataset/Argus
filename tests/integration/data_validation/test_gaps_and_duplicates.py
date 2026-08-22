"""Gap and duplicate detection against deliberately-broken fixture data."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from core.data_validation.duplicates import detect_duplicate_bars
from core.data_validation.gaps import detect_gaps
from data.canonical_model.pit import session_close
from data.canonical_model.records import CanonicalTimeframe
from infra.db.schema.canonical import canonical_ohlcv


def _insert_bar(
    connection: Connection,
    security_id: UUID,
    bar_date: date,
    *,
    close: str = "100",
    observation_time: datetime | None = None,
) -> None:
    close_time = session_close(bar_date)
    observed = observation_time or close_time
    price = Decimal(close)
    connection.execute(
        canonical_ohlcv.insert().values(
            security_id=security_id,
            timeframe=CanonicalTimeframe.DAILY.value,
            event_time=close_time,
            observation_time=observed,
            availability_time=observed,
            ingestion_time=observed,
            open_raw=price,
            high_raw=price,
            low_raw=price,
            close_raw=price,
            volume_raw=1000,
        )
    )


# --------------------------------------------------------------------------
# Gap detection
# --------------------------------------------------------------------------


def test_a_complete_week_has_no_gaps(connection: Connection, security_id: UUID):
    for day in (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)):
        _insert_bar(connection, security_id, day)

    report = detect_gaps(connection, security_id, date(2024, 1, 2), date(2024, 1, 5))
    assert bool(report) is True
    assert report.missing_dates == ()


def test_a_deliberately_missing_trading_day_is_caught(connection: Connection, security_id: UUID):
    # Jan 2, 3, 5 present; Jan 4 (a Thursday, an ordinary trading day) missing.
    for day in (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 5)):
        _insert_bar(connection, security_id, day)

    report = detect_gaps(connection, security_id, date(2024, 1, 2), date(2024, 1, 5))
    assert bool(report) is False
    assert report.missing_dates == (date(2024, 1, 4),)


def test_a_holiday_is_not_flagged_as_a_gap(connection: Connection, security_id: UUID):
    """Missing data on a real market holiday is expected, not an error."""
    for day in (date(2023, 12, 22), date(2023, 12, 26)):  # skips Dec 25 (Christmas)
        _insert_bar(connection, security_id, day)

    report = detect_gaps(connection, security_id, date(2023, 12, 22), date(2023, 12, 26))
    assert bool(report) is True


def test_gap_report_counts_expected_and_present_days(connection: Connection, security_id: UUID):
    _insert_bar(connection, security_id, date(2024, 1, 2))
    # Jan 3, 4, 5 missing.

    report = detect_gaps(connection, security_id, date(2024, 1, 2), date(2024, 1, 5))
    assert report.expected_count == 4
    assert report.present_count == 1
    assert len(report.missing_dates) == 3


def test_gap_detection_is_advisory_only_never_raises(connection: Connection, security_id: UUID):
    """No ingested data at all is a fact to report, not an exception."""
    report = detect_gaps(connection, security_id, date(2024, 1, 2), date(2024, 1, 5))
    assert len(report.missing_dates) == 4


# --------------------------------------------------------------------------
# Duplicate detection
# --------------------------------------------------------------------------


def test_identical_restatement_rows_are_flagged_as_duplicates(
    connection: Connection, security_id: UUID
):
    """Two rows, same event, same value: a spurious re-ingestion, not a real correction."""
    _insert_bar(
        connection,
        security_id,
        date(2024, 1, 3),
        close="184.25",
        observation_time=datetime(2024, 1, 3, 22, 0, tzinfo=UTC),
    )
    _insert_bar(
        connection,
        security_id,
        date(2024, 1, 3),
        close="184.25",  # identical value
        observation_time=datetime(2024, 1, 10, 12, 0, tzinfo=UTC),  # re-fetched later
    )

    duplicates = detect_duplicate_bars(connection, security_id)
    assert len(duplicates) == 1
    assert duplicates[0].bar_date == date(2024, 1, 3)
    assert len(duplicates[0].observation_times) == 2


def test_a_genuine_restatement_with_a_different_value_is_not_a_duplicate(
    connection: Connection, security_id: UUID
):
    """A real correction is exactly what the PIT layer's restatement handling serves."""
    _insert_bar(
        connection,
        security_id,
        date(2024, 1, 3),
        close="184.25",
        observation_time=datetime(2024, 1, 3, 22, 0, tzinfo=UTC),
    )
    _insert_bar(
        connection,
        security_id,
        date(2024, 1, 3),
        close="184.50",  # a real correction, different value
        observation_time=datetime(2024, 1, 10, 12, 0, tzinfo=UTC),
    )

    duplicates = detect_duplicate_bars(connection, security_id)
    assert duplicates == []


def test_a_single_row_per_bar_is_never_flagged(connection: Connection, security_id: UUID):
    _insert_bar(connection, security_id, date(2024, 1, 3))
    assert detect_duplicate_bars(connection, security_id) == []


def test_duplicate_detection_does_not_delete_or_modify_anything(
    connection: Connection, security_id: UUID
):
    """Advisory: reported, never pruned — the append-only guard would refuse it anyway."""
    _insert_bar(
        connection,
        security_id,
        date(2024, 1, 3),
        close="184.25",
        observation_time=datetime(2024, 1, 3, 22, 0, tzinfo=UTC),
    )
    _insert_bar(
        connection,
        security_id,
        date(2024, 1, 3),
        close="184.25",
        observation_time=datetime(2024, 1, 10, 12, 0, tzinfo=UTC),
    )

    detect_duplicate_bars(connection, security_id)

    count = connection.execute(
        select(func.count())
        .select_from(canonical_ohlcv)
        .where(canonical_ohlcv.c.security_id == security_id)
    ).scalar_one()
    assert count == 2
