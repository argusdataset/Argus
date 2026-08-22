"""Refusing to compute statistics that a small sample cannot support.

The failure this guards against is quiet and specific: two analogues, one
+40% and one −10%, median +15%. Arithmetically correct, renders
beautifully, and worthless. Sitting next to a statistic from 200 cases it
is indistinguishable, and a reader has no way to tell which is which.

So `INSUFFICIENT` returns `None` for every statistic — deliberate
friction, forcing a downstream module to handle the absence rather than
letting it read a two-sample median as evidence. Same discipline as
Module 08's `None`-never-`0.0` and Module 09's `INSUFFICIENT_EVIDENCE`.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from core.historical_similarity.config import SimilarityThresholds
from core.historical_similarity.statistics import (
    SampleSufficiency,
    assess_sufficiency,
    summarize,
)
from infra.db.enums import MarketState, OutcomeStatus

THRESHOLDS = SimilarityThresholds()


def _outcomes(
    n: int,
    *,
    status: OutcomeStatus = OutcomeStatus.SUCCESS,
    realized: float = 0.20,
    regime: MarketState = MarketState.UPTREND,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "outcome_status": status.value,
                "realized_return": realized,
                "mfe": 0.35,
                "mae": -0.08,
                "time_to_mfe": timedelta(days=40),
                "market_regime_at_outcome": regime.value,
            }
            for _ in range(n)
        ]
    )


# --------------------------------------------------------------------------
# The refusal
# --------------------------------------------------------------------------


def test_two_analogues_produce_no_statistics_at_all():
    """The headline case. A median of two numbers is arithmetic, not evidence."""
    stats = summarize(_outcomes(2), THRESHOLDS)

    assert stats.sufficiency is SampleSufficiency.INSUFFICIENT
    assert stats.sample_count == 2
    assert stats.median_outcome is None
    assert stats.average_outcome is None
    assert stats.failure_rate is None
    assert stats.mfe is None
    assert stats.mae is None
    assert not stats.reportable


def test_an_empty_analogue_set_is_insufficient_not_a_crash():
    """The normal case before Module 17 runs."""
    stats = summarize(pd.DataFrame(), THRESHOLDS)

    assert stats.sufficiency is SampleSufficiency.INSUFFICIENT
    assert stats.sample_count == 0
    assert stats.median_outcome is None


def test_the_count_is_always_reported_even_when_statistics_are_not():
    """ "We found 2" is honest; "the median of those 2" is not.

    The count is a fact about the search. The statistics are inferences
    from it, and only the inferences are suppressed.
    """
    assert summarize(_outcomes(3), THRESHOLDS).sample_count == 3


def test_crossing_the_floor_turns_statistics_on():
    below = summarize(_outcomes(4), THRESHOLDS)
    at = summarize(_outcomes(5), THRESHOLDS)

    assert below.median_outcome is None
    assert at.median_outcome == pytest.approx(0.20)


# --------------------------------------------------------------------------
# Sparse versus adequate
# --------------------------------------------------------------------------


def test_a_sparse_sample_is_reported_and_flagged():
    """Between the floors: numbers, plus an explicit warning label."""
    stats = summarize(_outcomes(10), THRESHOLDS)

    assert stats.sufficiency is SampleSufficiency.SPARSE
    assert stats.median_outcome is not None
    assert stats.reportable


def test_a_large_sample_is_adequate():
    assert summarize(_outcomes(50), THRESHOLDS).sufficiency is SampleSufficiency.ADEQUATE


def test_sufficiency_bands_are_contiguous():
    """No count falls outside all three bands."""
    for count in range(0, 60):
        assert assess_sufficiency(count, THRESHOLDS) in set(SampleSufficiency)


# --------------------------------------------------------------------------
# Intervals accompany every point estimate
# --------------------------------------------------------------------------


def test_a_small_sample_interval_is_wide_enough_to_be_a_warning():
    """The interval is the honest signal, not a decoration.

    Ten cases split evenly gives a failure-rate interval so wide it spans
    most of the plausible range — which is the point. A consumer that
    reads the rate without the interval is reading half the answer.
    """
    mixed = pd.concat(
        [_outcomes(5, status=OutcomeStatus.SUCCESS), _outcomes(5, status=OutcomeStatus.FAILED)]
    )
    stats = summarize(mixed, THRESHOLDS)

    assert stats.failure_rate == pytest.approx(0.5)
    assert stats.failure_rate_interval is not None
    assert stats.failure_rate_interval.width > 0.5


def test_the_interval_narrows_as_the_sample_grows():
    small = summarize(
        pd.concat(
            [_outcomes(5, status=OutcomeStatus.SUCCESS), _outcomes(5, status=OutcomeStatus.FAILED)]
        ),
        THRESHOLDS,
    )
    large = summarize(
        pd.concat(
            [
                _outcomes(100, status=OutcomeStatus.SUCCESS),
                _outcomes(100, status=OutcomeStatus.FAILED),
            ]
        ),
        THRESHOLDS,
    )

    assert large.failure_rate_interval.width < small.failure_rate_interval.width


def test_the_failure_rate_interval_never_leaves_zero_to_one():
    """Why Wilson rather than the normal approximation.

    The normal approximation happily produces intervals extending below
    zero at small N or extreme rates — nonsense for a proportion, and
    exactly the region a failure rate lives in.
    """
    all_failed = summarize(_outcomes(6, status=OutcomeStatus.FAILED), THRESHOLDS)

    assert all_failed.failure_rate == pytest.approx(1.0)
    assert all_failed.failure_rate_interval.low >= 0.0
    assert all_failed.failure_rate_interval.high <= 1.0


# --------------------------------------------------------------------------
# What counts as a failure
# --------------------------------------------------------------------------


def test_an_expired_setup_is_not_counted_as_a_failure():
    """It did not fail — it did not conclude.

    Folding indecision into a failure rate would systematically overstate
    failure, and `OutcomeStatus` deliberately keeps EXPIRED distinct from
    FAILED precisely so this distinction survives.
    """
    mixed = pd.concat(
        [
            _outcomes(5, status=OutcomeStatus.SUCCESS),
            _outcomes(5, status=OutcomeStatus.EXPIRED),
        ]
    )
    stats = summarize(mixed, THRESHOLDS)

    assert stats.failure_rate == pytest.approx(0.0)
    assert stats.resolved_count == 5, "only resolved cases enter the denominator"


def test_unresolvable_outcomes_are_excluded_from_the_denominator():
    mixed = pd.concat(
        [
            _outcomes(5, status=OutcomeStatus.FAILED),
            _outcomes(5, status=OutcomeStatus.INVALIDATED),
            _outcomes(5, status=OutcomeStatus.NO_VALID_OUTCOME),
        ]
    )
    stats = summarize(mixed, THRESHOLDS)

    assert stats.resolved_count == 5
    assert stats.failure_rate == pytest.approx(1.0)


def test_a_set_with_no_resolved_outcomes_reports_no_failure_rate():
    """Not zero — unknown. Zero would read as "nothing failed"."""
    stats = summarize(_outcomes(8, status=OutcomeStatus.EXPIRED), THRESHOLDS)
    assert stats.failure_rate is None
    assert stats.resolved_count == 0


# --------------------------------------------------------------------------
# Distributions
# --------------------------------------------------------------------------


def test_expansion_magnitude_covers_only_successful_setups():
    """ "How far did it run when it ran" is a different question from the mean."""
    mixed = pd.concat(
        [_outcomes(6, status=OutcomeStatus.SUCCESS), _outcomes(6, status=OutcomeStatus.FAILED)]
    )
    stats = summarize(mixed, THRESHOLDS)

    assert stats.mfe.count == 12
    assert stats.expansion_magnitude.count == 6


def test_time_to_expansion_is_reported_in_days():
    stats = summarize(_outcomes(8), THRESHOLDS)
    assert stats.time_to_expansion.percentiles[50] == pytest.approx(40.0)


def test_every_distribution_carries_its_own_sample_size():
    """A distribution built from 3 of 30 cases must say so."""
    stats = summarize(_outcomes(30), THRESHOLDS)
    for distribution in (stats.mfe, stats.mae, stats.expansion_magnitude):
        assert distribution.count == 30


# --------------------------------------------------------------------------
# Regime splits
# --------------------------------------------------------------------------


def test_splitting_by_regime_re_applies_the_sufficiency_floor():
    """Splitting a small sample makes every bucket smaller.

    A set that is SPARSE overall is often INSUFFICIENT once split, and
    reporting a per-regime median from two cases would be the same error
    this module refuses to make at the top level.
    """
    mixed = pd.concat(
        [
            _outcomes(8, regime=MarketState.UPTREND),
            _outcomes(2, regime=MarketState.DOWN_TREND),
        ]
    )
    buckets = summarize(mixed, THRESHOLDS).outcome_by_regime

    assert buckets["UPTREND"]["median_outcome"] is not None
    assert buckets["DOWN_TREND"]["count"] == 2
    assert buckets["DOWN_TREND"]["sufficiency"] == SampleSufficiency.INSUFFICIENT.value
    assert buckets["DOWN_TREND"]["median_outcome"] is None
