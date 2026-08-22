"""The trading-day calendar, checked against known real dates."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.data_validation.calendar import expected_trading_days, is_trading_day, us_market_holidays


@pytest.mark.parametrize(
    "day",
    [
        date(2024, 1, 1),  # New Year's Day
        date(2024, 1, 15),  # MLK Day (3rd Monday)
        date(2024, 2, 19),  # Presidents' Day (3rd Monday)
        date(2024, 3, 29),  # Good Friday 2024
        date(2024, 5, 27),  # Memorial Day (last Monday)
        date(2024, 6, 19),  # Juneteenth
        date(2024, 7, 4),  # Independence Day
        date(2024, 9, 2),  # Labor Day (1st Monday)
        date(2024, 11, 28),  # Thanksgiving (4th Thursday)
        date(2024, 12, 25),  # Christmas
    ],
)
def test_known_2024_holidays_are_not_trading_days(day):
    assert not is_trading_day(day)


def test_a_saturday_holiday_is_observed_the_prior_friday():
    """July 4th 2026 falls on a Saturday; NYSE observes it on Friday July 3rd."""
    assert date(2026, 7, 4).weekday() == 5
    assert date(2026, 7, 3) in us_market_holidays(2026)


def test_a_sunday_holiday_is_observed_the_following_monday():
    """New Year's Day 2023 fell on a Sunday; NYSE observed it on Monday Jan 2."""
    assert date(2023, 1, 1).weekday() == 6
    assert date(2023, 1, 2) in us_market_holidays(2023)


@pytest.mark.parametrize(
    "day",
    [date(2024, 1, 2), date(2024, 6, 3), date(2024, 12, 26), date(2024, 3, 28)],
)
def test_ordinary_weekdays_are_trading_days(day):
    assert is_trading_day(day)


@pytest.mark.parametrize("day", [date(2024, 1, 6), date(2024, 1, 7)])  # Sat, Sun
def test_weekends_are_not_trading_days(day):
    assert not is_trading_day(day)


@pytest.mark.parametrize(
    ("year", "expected_easter"),
    [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)), (2019, date(2019, 4, 21))],
)
def test_good_friday_is_two_days_before_the_known_easter_sunday(year, expected_easter):
    good_friday = expected_easter - timedelta(days=2)
    assert good_friday in us_market_holidays(year)


def test_expected_trading_days_excludes_weekends_and_holidays():
    days = expected_trading_days(date(2024, 12, 23), date(2024, 12, 27))
    # Dec 23 (Mon), 24 (Tue) trading; 25 (Wed, Christmas) excluded;
    # 26 (Thu), 27 (Fri) trading.
    assert days == [date(2024, 12, 23), date(2024, 12, 24), date(2024, 12, 26), date(2024, 12, 27)]


def test_expected_trading_days_is_empty_for_a_reversed_range():
    assert expected_trading_days(date(2024, 6, 1), date(2024, 5, 1)) == []


def test_expected_trading_days_is_empty_for_a_single_weekend_day():
    assert expected_trading_days(date(2024, 1, 6), date(2024, 1, 6)) == []
