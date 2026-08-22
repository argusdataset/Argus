"""A US equity market trading-day calendar, for gap detection only.

Computed algorithmically for any year in ARGUS's ~30-year window, rather
than a hardcoded per-year lookup table. Deliberately approximate in one
documented way: **early closes (half-days) are not modelled** — the day
itself is still a trading day, which is all gap detection needs to know.
This mirrors Module 05's `session_close` docstring, which made the same
call for the same reason: the difference is a few hours on a handful of
dates each year, and it never affects whether a day counts as a session.

This is not a general-purpose market calendar library and isn't meant to
become one. It answers exactly one question — was date D a US equity
trading day — for exactly one purpose — telling a genuine data gap apart
from an expected non-trading day.
"""

from __future__ import annotations

from datetime import date, timedelta

#: New Year's, Juneteenth, Independence Day, and Christmas are fixed
#: calendar dates (with a weekend-observed shift); the rest are defined by
#: weekday rules below.
_FIXED_HOLIDAYS: tuple[tuple[int, int], ...] = (
    (1, 1),  # New Year's Day
    (6, 19),  # Juneteenth (observed by NYSE/NASDAQ since 2022)
    (7, 4),  # Independence Day
    (12, 25),  # Christmas
)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth occurrence of `weekday` (Mon=0) in `month`, e.g. 3rd Monday."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last occurrence of `weekday` in `month`, e.g. last Monday of May."""
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = next_month - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Easter Sunday via the Anonymous Gregorian algorithm.

    Needed only to derive Good Friday, the one US market holiday whose
    date isn't a fixed calendar date or a simple weekday rule.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _good_friday(year: int) -> date:
    return _easter_sunday(year) - timedelta(days=2)


def _observed(holiday: date) -> date:
    """NYSE/NASDAQ shift: Saturday -> observed Friday, Sunday -> observed Monday."""
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def us_market_holidays(year: int) -> frozenset[date]:
    """US equity market holidays for one year, with weekend observance applied."""
    holidays = {_observed(date(year, month, day)) for month, day in _FIXED_HOLIDAYS}
    holidays.add(_nth_weekday(year, 1, 0, 3))  # MLK Day: 3rd Monday of January
    holidays.add(_nth_weekday(year, 2, 0, 3))  # Presidents' Day: 3rd Monday of February
    holidays.add(_good_friday(year))
    holidays.add(_last_weekday(year, 5, 0))  # Memorial Day: last Monday of May
    holidays.add(_nth_weekday(year, 9, 0, 1))  # Labor Day: 1st Monday of September
    holidays.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving: 4th Thursday of November
    return frozenset(holidays)


def is_trading_day(day: date) -> bool:
    """Whether `day` is an expected US equity trading day.

    Not a certainty for any single date — an unscheduled market closure
    (a national day of mourning, a weather emergency) is not modelled and
    never will be from a formula. This is why gap detection is advisory:
    a formula-flagged "gap" on such a date is a false positive an operator
    resolves by eye, not a system that silently corrects itself.
    """
    if day.weekday() >= 5:
        return False
    return day not in us_market_holidays(day.year)


def expected_trading_days(start: date, end: date) -> list[date]:
    """Every expected trading day in `[start, end]`, inclusive."""
    if end < start:
        return []
    days = []
    current = start
    while current <= end:
        if is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days
