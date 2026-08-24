"""The metric suite: does it compute what it claims, and refuse what it cannot?

The tests that matter most here are the ones about absence. A precision of
0.0 and a precision of None mean opposite things — "every committed setup
failed" versus "ARGUS never committed to anything" — and a suite that
conflated them would be at its most confident exactly when it knew least.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.historical_similarity.statistics import SampleSufficiency
from core.model_validation_evaluation.evaluation.config import (
    EvaluationConfig,
    EvaluationThreshold,
    EvaluationThresholds,
)
from core.model_validation_evaluation.evaluation.metrics import (
    compute_metrics,
    confusion_matrix,
    max_drawdown,
)
from infra.db.enums import MarketState, OutcomeStatus
from tests.unit.model_validation_evaluation import factories as f


def low_floor(minimum: float = 2.0) -> EvaluationConfig:
    """A config whose sample floor lets a hand-built population report.

    Lowering the floor rather than building forty rows keeps each test's
    population small enough to read and reason about — the floor itself is
    tested separately, on purpose.
    """
    return EvaluationConfig(
        thresholds=EvaluationThresholds(
            min_bucket_sample=EvaluationThreshold(
                value=minimum, kind="calibratable", rationale="test"
            )
        )
    )


def test_the_confusion_matrix_counts_committed_against_succeeded():
    rows = [
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.SUCCESS),
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.FAILED),
        f.setup_row(argus_score=None, outcome_status=OutcomeStatus.FAILED),
        f.setup_row(argus_score=None, outcome_status=OutcomeStatus.SUCCESS),
    ]
    matrix = confusion_matrix(f.frame(rows))

    assert matrix.true_positive == 1
    assert matrix.false_positive == 1
    assert matrix.true_negative == 1
    assert matrix.false_negative == 1


def test_unresolved_setups_are_excluded_from_the_matrix_not_counted_as_losses():
    """Module 15's rule: EXPIRED did not fail, it did not conclude.

    Counting indecision as a loss would understate precision by exactly
    the fraction of setups that were still open when the window closed —
    which for a slow-base strategy is a large and systematically biased
    fraction.
    """
    rows = [
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.SUCCESS),
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.EXPIRED),
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.INVALIDATED),
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.NO_VALID_OUTCOME),
    ]
    suite = compute_metrics(f.frame(rows), low_floor())

    assert suite.confusion.total == 1
    assert suite.precision == 1.0
    # But the excluded ones stay visible rather than vanishing.
    assert suite.status_counts["EXPIRED"] == 1
    assert suite.status_counts["NO_VALID_OUTCOME"] == 1


def test_a_rate_with_no_denominator_is_none_and_not_zero():
    """The distinction the whole suite rests on."""
    rows = [
        f.setup_row(argus_score=None, outcome_status=OutcomeStatus.FAILED),
        f.setup_row(argus_score=None, outcome_status=OutcomeStatus.FAILED),
        f.setup_row(argus_score=None, outcome_status=OutcomeStatus.FAILED),
    ]
    suite = compute_metrics(f.frame(rows), low_floor())

    # Nothing was ever committed to, so precision has no denominator.
    assert suite.precision is None
    assert suite.expectancy is None
    # But the false positive rate does: three true negatives, no false
    # positives, which is a real 0.0 rather than an absent one.
    assert suite.false_positive_rate == 0.0


def test_hit_rate_and_precision_answer_different_questions():
    """A setup can beat its benchmark and still not meet the success bar.

    If these two ever collapse into one number, ARGUS has quietly
    redefined success as "beat SPY", which is not what Module 15 recorded.
    """
    rows = [
        # Rose relative to the benchmark, but Module 15 labelled it FAILED.
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.FAILED, relative_return=0.04),
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.FAILED, relative_return=0.03),
    ]
    suite = compute_metrics(f.frame(rows), low_floor())

    assert suite.precision == 0.0
    assert suite.hit_rate == 1.0


def test_every_return_statistic_is_benchmark_relative():
    """A gain in a market that gained more is not evidence.

    The fixture's absolute return is deliberately larger than its
    benchmark-relative one, so a suite reading the wrong column produces
    a visibly different expectancy.
    """
    rows = [f.setup_row(argus_score=80.0, relative_return=0.10) for _ in range(3)]
    suite = compute_metrics(f.frame(rows), low_floor())

    assert suite.expectancy == pytest.approx(0.10)
    # The factory sets realized_return = relative + 0.05.
    assert suite.expectancy != pytest.approx(0.15)
    assert suite.as_dict()["return_basis"] == "benchmark_relative"


def test_a_population_below_the_sample_floor_reports_a_count_and_nothing_else():
    rows = [f.setup_row(argus_score=80.0) for _ in range(3)]
    suite = compute_metrics(f.frame(rows), EvaluationConfig())

    assert suite.sample_size == 3
    assert suite.sufficiency is SampleSufficiency.INSUFFICIENT
    assert not suite.reportable
    assert suite.precision is None
    assert suite.expectancy is None
    assert suite.hit_rate is None
    # The raw counts survive, because "three setups, two succeeded" is a
    # fact; "67% precision" is the claim the floor exists to refuse.
    assert suite.confusion.true_positive == 3


def test_an_empty_population_is_insufficient_rather_than_an_error():
    suite = compute_metrics(f.frame([]), EvaluationConfig())

    assert suite.sample_size == 0
    assert suite.sufficiency is SampleSufficiency.INSUFFICIENT
    assert suite.confusion.total == 0


def test_drawdown_is_a_path_property_not_the_worst_single_setup():
    """Three winners then a loser has a drawdown; the worst MAE does not
    describe the same thing and is reported separately."""
    rows = [
        f.setup_row(argus_score=80.0, relative_return=0.10, mae=-0.30),
        f.setup_row(
            argus_score=80.0,
            relative_return=0.10,
            mae=-0.05,
            detected_at=f.BASE + timedelta(days=10),
        ),
        f.setup_row(
            argus_score=80.0,
            relative_return=-0.25,
            mae=-0.05,
            detected_at=f.BASE + timedelta(days=20),
        ),
    ]
    suite = compute_metrics(f.frame(rows), low_floor())

    assert suite.max_drawdown == pytest.approx(-0.25)
    assert suite.worst_single_mae == pytest.approx(-0.30)


def test_drawdown_orders_by_detection_date_not_by_row_order():
    """The rows arrive worst-first; the curve must still be chronological."""
    rows = [
        f.setup_row(
            argus_score=80.0,
            relative_return=-0.25,
            detected_at=f.BASE + timedelta(days=20),
        ),
        f.setup_row(argus_score=80.0, relative_return=0.10, detected_at=f.BASE),
        f.setup_row(
            argus_score=80.0,
            relative_return=0.10,
            detected_at=f.BASE + timedelta(days=10),
        ),
    ]

    assert max_drawdown(f.frame(rows)) == pytest.approx(-0.25)


def test_a_curve_that_only_rises_reports_zero_drawdown_which_is_a_measurement():
    rows = [
        f.setup_row(argus_score=80.0, relative_return=0.10, detected_at=f.BASE + timedelta(days=n))
        for n in range(3)
    ]

    assert max_drawdown(f.frame(rows)) == pytest.approx(0.0)


def test_recall_counts_the_successes_argus_declined_to_commit_to():
    """The metric only computable because detection does not require a score.

    Module 14's bootstrap decision is what makes this measurable at all:
    setups exist that ARGUS noticed and never qualified, and some of them
    succeeded. Without those rows recall would be unknowable and this
    number would have to be a guess.
    """
    rows = [
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.SUCCESS),
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.SUCCESS),
        f.setup_row(argus_score=None, outcome_status=OutcomeStatus.SUCCESS),
        f.setup_row(argus_score=None, outcome_status=OutcomeStatus.SUCCESS),
    ]
    suite = compute_metrics(f.frame(rows), low_floor())

    assert suite.recall == pytest.approx(0.5)
    assert suite.precision == pytest.approx(1.0)


def test_every_rate_carries_an_interval_so_a_thin_sample_looks_thin():
    rows = [
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.SUCCESS),
        f.setup_row(argus_score=80.0, outcome_status=OutcomeStatus.FAILED),
    ]
    suite = compute_metrics(f.frame(rows), low_floor())

    assert suite.precision == pytest.approx(0.5)
    assert suite.precision_interval is not None
    # Two samples cannot pin a rate; the interval says so rather than the
    # point estimate implying a precision it does not have.
    assert suite.precision_interval.width > 0.5


def test_the_mfe_mae_ratio_is_a_ratio_of_means_not_a_mean_of_ratios():
    """One calm entry must not dominate the statistic.

    The second setup's MAE is near zero, so its per-setup ratio is ~100.
    A mean of ratios would report roughly 50; a ratio of means reports
    something the population actually supports.
    """
    rows = [
        f.setup_row(argus_score=80.0, mfe=0.20, mae=-0.20),
        f.setup_row(argus_score=80.0, mfe=0.20, mae=-0.002),
    ]
    suite = compute_metrics(f.frame(rows), low_floor())

    assert suite.mfe_mae_ratio == pytest.approx(0.20 / 0.101, rel=1e-3)
    assert suite.mfe_mae_ratio < 3.0


def test_the_regime_column_travels_into_the_suites_status_counts_unchanged():
    rows = [
        f.setup_row(argus_score=80.0, regime=MarketState.UPTREND),
        f.setup_row(argus_score=80.0, regime=MarketState.DOWN_TREND),
    ]
    suite = compute_metrics(f.frame(rows), low_floor())

    assert suite.status_counts == {"SUCCESS": 2}
