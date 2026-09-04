"""The tri-state decision, in isolation.

Unit tests deliberately: `evaluate` takes plain counts, not a connection
(see the module docstring on why), so every case here is exercised without
a database. The PIT-correct aggregate query behind those counts is tested
against a real PostgreSQL in `tests/integration/news_signals/`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from core.data_validation.result import MissReason
from core.news_signals.config import CALIBRATABLE, NewsSignalThreshold, NewsSignalThresholds
from core.news_signals.signal import evaluate

AS_OF = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)
SCAN_DATE = date(2024, 6, 3)


@pytest.fixture
def thresholds() -> NewsSignalThresholds:
    return NewsSignalThresholds()


def _evaluate(
    *,
    today_count: int,
    baseline_total: int,
    earliest_known_event_date: date | None,
    thresholds: NewsSignalThresholds,
):
    return evaluate(
        security_id=uuid4(),
        scan_date=SCAN_DATE,
        as_of=AS_OF,
        today_count=today_count,
        baseline_total=baseline_total,
        earliest_known_event_date=earliest_known_event_date,
        thresholds=thresholds,
        config_version_label="test-version",
    )


# --------------------------------------------------------------------------
# Undetermined: no history at all
# --------------------------------------------------------------------------


def test_a_security_with_no_news_ever_is_undetermined_not_false(thresholds):
    """`raised=None`, never coerced to `False` — the whole point of the
    three-state pattern this module copies from `core/risk_context/flags.py`."""
    signal = _evaluate(
        today_count=0, baseline_total=0, earliest_known_event_date=None, thresholds=thresholds
    )

    assert signal.raised is None
    assert signal.determined is False
    assert signal.unavailable is MissReason.NEVER_INGESTED
    assert signal.baseline_mean is None


def test_never_ingested_is_reported_even_when_todays_count_is_nonzero(thresholds):
    """A malformed backfill could put a row in today's bucket with no
    earlier history; the absence of a baseline still wins over a raw count."""
    signal = _evaluate(
        today_count=5, baseline_total=0, earliest_known_event_date=None, thresholds=thresholds
    )

    assert signal.raised is None
    assert signal.unavailable is MissReason.NEVER_INGESTED


# --------------------------------------------------------------------------
# Undetermined: history exists but the window is not full yet
# --------------------------------------------------------------------------


def test_a_security_covered_for_fewer_days_than_the_window_is_undetermined(thresholds):
    """First article nine days ago; a thirty-day baseline is not observable."""
    earliest = date(2024, 5, 25)  # 9 days before SCAN_DATE
    signal = _evaluate(
        today_count=3, baseline_total=2, earliest_known_event_date=earliest, thresholds=thresholds
    )

    assert signal.raised is None
    assert signal.unavailable is MissReason.NOT_YET_AVAILABLE
    assert signal.baseline_mean is None
    assert signal.detail["days_observed"] == 9


def test_exactly_a_full_window_of_history_is_determinable(thresholds):
    """An earliest article exactly `window_days` before `scan_date` has
    accumulated a complete trailing window — the boundary belongs to the
    determinable side, not the undetermined one."""
    earliest = SCAN_DATE - thresholds.window  # exactly window_days back

    signal = _evaluate(
        today_count=1, baseline_total=1, earliest_known_event_date=earliest, thresholds=thresholds
    )

    assert signal.determined is True
    assert signal.unavailable is None


def test_one_day_short_of_a_full_window_is_still_undetermined(thresholds):
    """One calendar day inside the boundary: the window is not full yet."""
    from datetime import timedelta

    window_days = thresholds.window_days
    earliest = SCAN_DATE - thresholds.window + timedelta(days=1)

    signal = _evaluate(
        today_count=1, baseline_total=1, earliest_known_event_date=earliest, thresholds=thresholds
    )

    assert signal.raised is None
    assert signal.unavailable is MissReason.NOT_YET_AVAILABLE
    assert signal.detail["days_observed"] == window_days - 1


# --------------------------------------------------------------------------
# Determined: the relative-multiple decision
# --------------------------------------------------------------------------


def _long_covered(thresholds: NewsSignalThresholds) -> date:
    from datetime import timedelta

    return SCAN_DATE - thresholds.window - timedelta(days=365)


def test_ordinary_volume_does_not_raise(thresholds):
    """Baseline mean 2.0/day, multiple 3x -> threshold 6.0; today's 3 stays below it."""
    signal = _evaluate(
        today_count=3,
        baseline_total=2 * thresholds.window_days,
        earliest_known_event_date=_long_covered(thresholds),
        thresholds=thresholds,
    )

    assert signal.raised is False
    assert signal.determined is True
    assert signal.baseline_mean == pytest.approx(2.0)
    assert signal.unavailable is None


def test_volume_at_the_multiple_threshold_raises(thresholds):
    """The comparison is >=, not >: exactly at the threshold counts as raised."""
    baseline_mean = 2.0
    threshold = thresholds.multiple * baseline_mean
    signal = _evaluate(
        today_count=int(threshold),
        baseline_total=int(baseline_mean * thresholds.window_days),
        earliest_known_event_date=_long_covered(thresholds),
        thresholds=thresholds,
    )

    assert signal.raised is True


def test_volume_well_above_the_multiple_raises(thresholds):
    signal = _evaluate(
        today_count=7,
        baseline_total=2 * thresholds.window_days,
        earliest_known_event_date=_long_covered(thresholds),
        thresholds=thresholds,
    )

    assert signal.raised is True
    assert signal.determined is True


# --------------------------------------------------------------------------
# The deliberate zero-baseline fallback
# --------------------------------------------------------------------------


def test_a_zero_baseline_with_no_articles_today_does_not_raise(thresholds):
    signal = _evaluate(
        today_count=0,
        baseline_total=0,
        earliest_known_event_date=_long_covered(thresholds),
        thresholds=thresholds,
    )

    assert signal.determined is True
    assert signal.raised is False
    assert signal.baseline_mean == pytest.approx(0.0)


def test_a_zero_baseline_with_any_coverage_today_raises(thresholds):
    """Documented, deliberate behaviour: any coverage at all is unusual for
    a name that normally has none. See `core/news_signals/config.py` on why
    no absolute floor is added on top of this."""
    signal = _evaluate(
        today_count=1,
        baseline_total=0,
        earliest_known_event_date=_long_covered(thresholds),
        thresholds=thresholds,
    )

    assert signal.determined is True
    assert signal.raised is True
    assert signal.detail["zero_baseline_fallback"] is True


# --------------------------------------------------------------------------
# The configuration travels with the verdict
# --------------------------------------------------------------------------


def test_a_stored_verdict_carries_the_thresholds_it_applied(thresholds):
    signal = _evaluate(
        today_count=3,
        baseline_total=2 * thresholds.window_days,
        earliest_known_event_date=_long_covered(thresholds),
        thresholds=thresholds,
    )

    assert signal.baseline_window_days == thresholds.window_days
    assert signal.multiple_threshold == pytest.approx(thresholds.multiple)
    assert signal.config_version_label == "test-version"


def test_the_multiple_is_read_from_the_configuration_not_hardcoded(thresholds):
    """Move the threshold and the verdict must move, the same discipline
    `test_flags.py` applies to `core/risk_context`."""
    baseline_total = 2 * thresholds.window_days  # baseline_mean == 2.0
    earliest = _long_covered(thresholds)

    narrow = NewsSignalThresholds(
        anomaly_multiple=NewsSignalThreshold(value=1.5, kind=CALIBRATABLE, rationale="test")
    )
    wide = NewsSignalThresholds(
        anomaly_multiple=NewsSignalThreshold(value=10.0, kind=CALIBRATABLE, rationale="test")
    )

    raised_narrow = _evaluate(
        today_count=3,
        baseline_total=baseline_total,
        earliest_known_event_date=earliest,
        thresholds=narrow,
    ).raised
    raised_wide = _evaluate(
        today_count=3,
        baseline_total=baseline_total,
        earliest_known_event_date=earliest,
        thresholds=wide,
    ).raised

    assert raised_narrow is True
    assert raised_wide is False


def test_a_determined_signal_never_carries_a_miss_reason(thresholds):
    """The converse of the undetermined cases: a real verdict must not be
    filterable-out as if it were absent evidence."""
    signal = _evaluate(
        today_count=3,
        baseline_total=2 * thresholds.window_days,
        earliest_known_event_date=_long_covered(thresholds),
        thresholds=thresholds,
    )

    assert signal.raised is not None
    assert signal.unavailable is None


def test_an_undetermined_signal_always_names_why(thresholds):
    for signal in (
        _evaluate(
            today_count=0, baseline_total=0, earliest_known_event_date=None, thresholds=thresholds
        ),
        _evaluate(
            today_count=0,
            baseline_total=0,
            earliest_known_event_date=date(2024, 6, 1),
            thresholds=thresholds,
        ),
    ):
        assert signal.raised is None
        assert signal.unavailable is not None
        assert signal.baseline_mean is None
