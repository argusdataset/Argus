"""Walk-forward: do the rolls actually roll, and does instability read as instability?

The mechanism's whole value is that consecutive windows are genuinely
different periods. A "walk-forward" whose windows all covered the same
years would produce identical numbers and read as reassuring consistency,
which is the most expensive way this could fail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.model_validation_evaluation.evaluation.config import (
    EvaluationConfig,
    EvaluationThreshold,
    EvaluationThresholds,
)
from core.model_validation_evaluation.evaluation.walk_forward import (
    build_windows,
    walk_forward,
)
from infra.db.enums import OutcomeStatus
from tests.unit.model_validation_evaluation import factories as f

START = datetime(2015, 1, 1, tzinfo=UTC)
END = datetime(2023, 1, 1, tzinfo=UTC)


def fast_rolls(*, train_days: float = 365.0, test_days: float = 365.0) -> EvaluationConfig:
    """Shorter windows and a low sample floor, so a fixture can fill them."""
    return EvaluationConfig(
        thresholds=EvaluationThresholds(
            walk_forward_train_days=EvaluationThreshold(
                value=train_days, kind="calibratable", rationale="test"
            ),
            walk_forward_test_days=EvaluationThreshold(
                value=test_days, kind="calibratable", rationale="test"
            ),
            walk_forward_step_days=EvaluationThreshold(
                value=test_days, kind="calibratable", rationale="test"
            ),
            min_bucket_sample=EvaluationThreshold(value=3.0, kind="calibratable", rationale="test"),
        )
    )


def test_the_windows_are_genuinely_different_periods():
    windows = build_windows(START, END)

    assert len(windows) >= 2
    starts = [window.train_start for window in windows]
    assert len(set(starts)) == len(starts)
    # Each roll moves forward by the configured step, not by zero.
    assert windows[1].train_start - windows[0].train_start == timedelta(days=365)
    assert windows[1].test_start > windows[0].test_end - timedelta(days=1)


def test_consecutive_test_periods_tile_rather_than_overlap():
    """Overlapping tests would count one outcome in several rolls and make
    the rolls look more independent than they are."""
    windows = build_windows(START, END)

    for earlier, later in zip(windows, windows[1:], strict=False):
        assert later.test_start >= earlier.test_end


def test_train_and_test_never_overlap_within_a_window():
    for window in build_windows(START, END):
        assert window.test_start >= window.train_end
        assert window.train_start < window.train_end


def test_a_window_whose_test_period_runs_past_the_period_end_is_not_emitted():
    """A truncated final window would be measured on less data and then
    compared against the others as an equal."""
    windows = build_windows(START, START + timedelta(days=1200))

    assert all(window.test_end <= START + timedelta(days=1200) for window in windows)


def test_a_period_shorter_than_one_window_produces_none():
    assert build_windows(START, START + timedelta(days=30)) == []


def test_a_period_too_short_for_the_minimum_rolls_is_undetermined_and_says_why():
    dataset = f.dataset(f.mixed_population())
    analysis = walk_forward(dataset, period_start=START, period_end=START + timedelta(days=30))

    assert analysis.stable is None
    assert "window(s) fit in the period" in analysis.undetermined_reason


def test_a_consistently_positive_model_reads_as_stable():
    rows = [
        f.setup_row(
            detected_at=START + timedelta(days=30 * n),
            argus_score=80.0,
            relative_return=0.15,
            outcome_status=OutcomeStatus.SUCCESS,
        )
        for n in range(90)
    ]
    analysis = walk_forward(
        f.dataset(rows), period_start=START, period_end=END, config=fast_rolls()
    )

    assert analysis.reportable_rolls >= 2
    assert analysis.stable is True
    assert analysis.consistent_direction is True
    assert analysis.expectancy_range[0] > 0


def test_two_good_years_and_one_catastrophic_one_is_unstable_not_a_positive_average():
    """The exact failure walk-forward exists to expose.

    The mean across rolls is positive here. Reporting that mean as the
    result would describe a model that lost badly in one regime as one
    that works.
    """
    rows = []
    for n in range(120):
        moment = START + timedelta(days=25 * n)
        collapsed = datetime(2018, 1, 1, tzinfo=UTC) <= moment < datetime(2019, 6, 1, tzinfo=UTC)
        rows.append(
            f.setup_row(
                detected_at=moment,
                argus_score=80.0,
                relative_return=-0.45 if collapsed else 0.18,
                outcome_status=(OutcomeStatus.FAILED if collapsed else OutcomeStatus.SUCCESS),
            )
        )
    analysis = walk_forward(
        f.dataset(rows), period_start=START, period_end=END, config=fast_rolls()
    )

    assert analysis.mean_test_expectancy > 0
    assert analysis.consistent_direction is False
    assert analysis.stable is False
    assert analysis.expectancy_range[0] < 0 < analysis.expectancy_range[1]


def test_a_consistently_losing_model_is_consistent_but_not_stable():
    """ "Stable" must not be readable as "reliable in the direction you want"."""
    rows = [
        f.setup_row(
            detected_at=START + timedelta(days=25 * n),
            argus_score=80.0,
            relative_return=-0.12,
            outcome_status=OutcomeStatus.FAILED,
        )
        for n in range(120)
    ]
    analysis = walk_forward(
        f.dataset(rows), period_start=START, period_end=END, config=fast_rolls()
    )

    assert analysis.consistent_direction is True
    assert analysis.stable is False


def test_each_roll_measures_a_different_population():
    """If two rolls ever measured the same setups, the slicing is broken."""
    rows = [
        f.setup_row(
            detected_at=START + timedelta(days=25 * n),
            argus_score=80.0,
            relative_return=0.02 * n,
        )
        for n in range(120)
    ]
    analysis = walk_forward(
        f.dataset(rows), period_start=START, period_end=END, config=fast_rolls()
    )
    reportable = [roll for roll in analysis.rolls if roll.reportable]

    expectancies = [roll.test.expectancy for roll in reportable]
    assert len(set(expectancies)) == len(expectancies)


def test_the_report_states_that_nothing_is_fitted_per_window():
    """ARGUS trains nothing today. The name must not imply otherwise."""
    analysis = walk_forward(f.dataset(f.mixed_population()), period_start=START, period_end=END)

    assert "No parameters are fitted per window" in analysis.caveat


def test_the_windows_change_when_the_configuration_changes():
    default_windows = build_windows(START, END)
    short_windows = build_windows(START, END, fast_rolls(train_days=180.0, test_days=180.0))

    assert len(short_windows) > len(default_windows)
    assert short_windows[0].train_end != default_windows[0].train_end


def test_the_dataset_slice_for_a_window_is_bounded_by_detection_date():
    dataset = f.dataset(
        [
            f.setup_row(detected_at=START, argus_score=80.0),
            f.setup_row(detected_at=START + timedelta(days=800), argus_score=80.0),
        ]
    )
    narrowed = dataset.within(START, START + timedelta(days=100))

    assert len(narrowed) == 1
    assert narrowed.period_start == START


def test_expectancy_across_rolls_is_reported_as_a_range_not_only_a_mean():
    rows = [
        f.setup_row(
            detected_at=START + timedelta(days=25 * n),
            argus_score=80.0,
            relative_return=0.30 if n % 2 else 0.02,
        )
        for n in range(120)
    ]
    analysis = walk_forward(
        f.dataset(rows), period_start=START, period_end=END, config=fast_rolls()
    )

    low, high = analysis.expectancy_range
    assert low <= analysis.mean_test_expectancy <= high
    assert "ranged" in analysis.summary()


def test_a_roll_whose_test_window_is_empty_is_not_counted_as_reportable():
    rows = [f.setup_row(detected_at=START + timedelta(days=n), argus_score=80.0) for n in range(40)]
    analysis = walk_forward(
        f.dataset(rows), period_start=START, period_end=END, config=fast_rolls()
    )

    assert analysis.reportable_rolls < len(analysis.rolls)
    assert analysis.stable is None or analysis.reportable_rolls >= 2


def test_the_minimum_window_count_is_structural_and_one_split_is_not_walk_forward():
    config = EvaluationConfig()

    assert config.thresholds.min_walk_forward_windows.kind == "structural"
    assert config.thresholds.min_walk_forward_windows.value == pytest.approx(2.0)
