"""Multi-timeframe derivation — and the refusal to fake H4.

The weekly/monthly tests check that a rollup invents nothing: every
derived value is a reduction of daily bars that actually exist.

The H4 tests are the more important half. `infra/db/README.md` records
that H4 is "derived from daily", and the honest reading of that is that it
*cannot be* — a daily bar carries no intraday structure. So the test
asserts a refusal, which is an unusual thing to want and is the point:
the alternative is a plausible-looking H4 series that is entirely
fabricated, and a downstream module has no way to tell one from the other.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.feature_engine.panel import PricePanel
from core.feature_engine.timeframes import (
    DERIVABLE_TIMEFRAMES,
    UnsupportedTimeframeError,
    derive_panel,
    periods_per_year,
)
from data.canonical_model.records import CanonicalTimeframe

SECURITY = "s0"


@pytest.fixture
def two_full_weeks() -> PricePanel:
    """Ten business days: Mon 6 Jan 2020 through Fri 17 Jan 2020.

    Chosen so both weeks are complete — the incomplete-period rule is
    tested separately, and mixing the two would make a failure ambiguous.
    """
    dates = pd.bdate_range("2020-01-06", "2020-01-17", tz="UTC")
    assert len(dates) == 10

    def wide(values: list[float]) -> pd.DataFrame:
        return pd.DataFrame({SECURITY: values}, index=dates)

    return PricePanel(
        open_adj=wide([10, 11, 12, 13, 14, 20, 21, 22, 23, 24]),
        high_adj=wide([15, 16, 17, 18, 19, 25, 30, 27, 28, 29]),
        low_adj=wide([5, 4, 3, 6, 7, 15, 16, 17, 12, 18]),
        close_adj=wide([11, 12, 13, 14, 15, 21, 22, 23, 24, 25]),
        close_raw=wide([11, 12, 13, 14, 15, 21, 22, 23, 24, 25]),
        volume=wide([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]),
        availability=pd.Series({SECURITY: pd.Timestamp("2020-02-01", tz="UTC")}),
    )


# --------------------------------------------------------------------------
# H4 — the refusal
# --------------------------------------------------------------------------


def test_h4_is_refused_rather_than_fabricated(two_full_weeks):
    """No number at all beats a made-up one.

    Forward-filling a daily bar into six H4 buckets produces five bars
    with a true range of zero; interpolating invents a smooth path that
    understates exactly the range and volatility features H4 would exist
    to measure. Both are fabrication, and neither is distinguishable from
    a real reading once it is stored in `feature_vectors`.
    """
    with pytest.raises(UnsupportedTimeframeError) as raised:
        derive_panel(two_full_weeks, CanonicalTimeframe.H4)

    message = str(raised.value)
    assert "intraday" in message
    assert "fabricated" in message


def test_h4_is_absent_from_the_derivable_list(two_full_weeks):
    """The refusal is declared, not just raised — callers can check first."""
    assert CanonicalTimeframe.H4 not in DERIVABLE_TIMEFRAMES
    assert set(DERIVABLE_TIMEFRAMES) == {
        CanonicalTimeframe.DAILY,
        CanonicalTimeframe.WEEKLY,
        CanonicalTimeframe.MONTHLY,
    }


# --------------------------------------------------------------------------
# Weekly and monthly rollups
# --------------------------------------------------------------------------


def test_daily_is_returned_unchanged(two_full_weeks):
    assert derive_panel(two_full_weeks, CanonicalTimeframe.DAILY) is two_full_weeks


def test_a_weekly_bar_is_a_reduction_of_the_days_beneath_it(two_full_weeks):
    """First open, max high, min low, last close, summed volume. No invention."""
    weekly = derive_panel(two_full_weeks, CanonicalTimeframe.WEEKLY)
    assert len(weekly.dates) == 2

    first, second = 0, 1
    assert weekly.open_adj.iloc[first][SECURITY] == 10  # Monday's open
    assert weekly.high_adj.iloc[first][SECURITY] == 19  # highest of the week
    assert weekly.low_adj.iloc[first][SECURITY] == 3  # lowest of the week
    assert weekly.close_adj.iloc[first][SECURITY] == 15  # Friday's close
    assert weekly.volume.iloc[first][SECURITY] == 1500  # sum of the week

    assert weekly.high_adj.iloc[second][SECURITY] == 30
    assert weekly.low_adj.iloc[second][SECURITY] == 12
    assert weekly.close_adj.iloc[second][SECURITY] == 25


def test_every_weekly_value_appears_in_the_daily_data(two_full_weeks):
    """Nothing derived is outside the range of what was observed."""
    weekly = derive_panel(two_full_weeks, CanonicalTimeframe.WEEKLY)
    assert weekly.high_adj.max().max() <= two_full_weeks.high_adj.max().max()
    assert weekly.low_adj.min().min() >= two_full_weeks.low_adj.min().min()


def test_an_incomplete_final_period_is_dropped(two_full_weeks):
    """A half-finished week is smaller purely because it is half-finished.

    Keeping it would put a systematically understated range and volume at
    the most recent — and most decision-relevant — bar of every rolling
    window. Weekly features therefore lag by up to a week, which is
    honest rather than convenient.
    """
    # Truncate to Mon-Wed of the second week: an unfinished period.
    partial = PricePanel(
        open_adj=two_full_weeks.open_adj.iloc[:8],
        high_adj=two_full_weeks.high_adj.iloc[:8],
        low_adj=two_full_weeks.low_adj.iloc[:8],
        close_adj=two_full_weeks.close_adj.iloc[:8],
        close_raw=two_full_weeks.close_raw.iloc[:8],
        volume=two_full_weeks.volume.iloc[:8],
        availability=two_full_weeks.availability,
    )
    weekly = derive_panel(partial, CanonicalTimeframe.WEEKLY)
    assert len(weekly.dates) == 1, "the unfinished second week must not be emitted"
    assert weekly.close_adj.iloc[0][SECURITY] == 15


def test_monthly_rolls_up_the_same_way(two_full_weeks):
    monthly = derive_panel(two_full_weeks, CanonicalTimeframe.MONTHLY)
    # January is not complete in this fixture, so nothing is emitted.
    assert len(monthly.dates) == 0


def test_availability_is_carried_through_a_rollup(two_full_weeks):
    """A derived bar is available no earlier than the daily bars that made it."""
    weekly = derive_panel(two_full_weeks, CanonicalTimeframe.WEEKLY)
    assert weekly.availability[SECURITY] == two_full_weeks.availability[SECURITY]


# --------------------------------------------------------------------------
# Annualisation
# --------------------------------------------------------------------------


def test_annualisation_matches_the_timeframe():
    """20 bars is 20 sessions daily and 20 weeks weekly — the scaling must follow."""
    assert periods_per_year(CanonicalTimeframe.DAILY) == 252
    assert periods_per_year(CanonicalTimeframe.WEEKLY) == 52
    assert periods_per_year(CanonicalTimeframe.MONTHLY) == 12
