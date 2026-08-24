"""Score monotonicity: it must confirm a real ordering *and* catch a fake one.

A monotonicity analysis that only ever passes is worse than none — it
supplies evidence for a claim it never tested. So every "it holds" test
here is paired with a population where it must not hold, and the pairing
is the point.
"""

from __future__ import annotations

import pytest

from core.model_validation_evaluation.evaluation.buckets import (
    analyse_monotonicity,
    build_buckets,
    calibration_curve,
)
from core.model_validation_evaluation.evaluation.config import (
    EvaluationConfig,
    EvaluationThreshold,
    EvaluationThresholds,
    ScoreBucketing,
)
from tests.unit.model_validation_evaluation import factories as f


def test_monotonicity_holds_on_a_population_where_the_score_orders_outcomes():
    analysis = analyse_monotonicity(f.frame(f.monotone_population()))

    assert analysis.monotonic is True
    assert analysis.reportable_buckets == 6
    assert analysis.violations == []
    assert analysis.spearman == pytest.approx(1.0)
    assert analysis.top_vs_bottom > 0.30


def test_monotonicity_fails_when_every_bucket_performs_identically():
    """The failure this analysis exists for.

    The pattern still works in this population — 12% relative return
    everywhere — and that is exactly why it is dangerous: the headline
    metrics look excellent while the score carries no information at all.
    """
    analysis = analyse_monotonicity(f.frame(f.flat_population()))

    assert analysis.monotonic is False
    assert analysis.top_vs_bottom == pytest.approx(0.0, abs=1e-9)
    # Every bucket identical is not a correlation and is not an error.
    assert analysis.spearman == 0.0
    assert "does not hold" in analysis.summary()


def test_monotonicity_fails_and_names_the_pairs_when_high_scores_do_worse():
    analysis = analyse_monotonicity(f.frame(f.inverted_population()))

    assert analysis.monotonic is False
    assert analysis.top_vs_bottom < 0
    assert analysis.spearman == pytest.approx(-1.0)
    # Five consecutive pairs, every one of them a violation, each named.
    assert len(analysis.violations) == 5
    assert analysis.violations[0].lower == "20-40"
    assert analysis.violations[0].shortfall > 0


def test_a_single_dip_inside_the_tolerance_is_not_called_a_violation():
    """Noise must not read as failure.

    Buckets rise overall, with one band a hair below its predecessor. At
    the default two-point tolerance that is noise; the verdict should
    still be that monotonicity holds.
    """
    rows = []
    for score, centre in [
        (30.0, 0.02),
        (50.0, 0.05),
        (65.0, 0.045),
        (75.0, 0.12),
        (85.0, 0.20),
        (95.0, 0.28),
    ]:
        rows += [
            f.setup_row(
                detected_at=f.BASE,
                argus_score=score,
                relative_return=centre,
            )
            for _ in range(25)
        ]
    analysis = analyse_monotonicity(f.frame(rows))

    assert analysis.violations == []
    assert analysis.monotonic is True


def test_the_same_dip_is_a_violation_once_the_tolerance_is_tightened():
    """The tolerance is a real knob, not a decoration.

    Same population as the test above; only `monotonicity_tolerance`
    changes. If this passed identically at both settings, the tolerance
    would not be doing anything and the config entry would be a lie.
    """
    rows = []
    for score, centre in [
        (30.0, 0.02),
        (50.0, 0.05),
        (65.0, 0.045),
        (75.0, 0.12),
        (85.0, 0.20),
        (95.0, 0.28),
    ]:
        rows += [
            f.setup_row(detected_at=f.BASE, argus_score=score, relative_return=centre)
            for _ in range(25)
        ]

    strict = EvaluationConfig(
        thresholds=EvaluationThresholds(
            monotonicity_tolerance=EvaluationThreshold(
                value=0.001, kind="calibratable", rationale="test"
            )
        )
    )
    analysis = analyse_monotonicity(f.frame(rows), strict)

    assert len(analysis.violations) == 1
    assert analysis.violations[0].lower == "40-60"
    assert analysis.violations[0].higher == "60-70"
    assert analysis.monotonic is False


def test_a_curve_that_rises_then_collapses_at_the_top_is_not_monotonic():
    """The case a correlation alone would forgive.

    Five rising buckets and a catastrophic top band still produce a
    positive-ish overall shape, and `top_vs_bottom` alone might survive.
    The violation check is what catches it — which is why the verdict
    requires both.
    """
    rows = []
    for score, centre in [
        (30.0, 0.02),
        (50.0, 0.06),
        (65.0, 0.10),
        (75.0, 0.16),
        (85.0, 0.24),
        (95.0, -0.20),
    ]:
        rows += [
            f.setup_row(detected_at=f.BASE, argus_score=score, relative_return=centre)
            for _ in range(25)
        ]
    analysis = analyse_monotonicity(f.frame(rows))

    assert analysis.monotonic is False
    assert [v.higher for v in analysis.violations] == ["90-100"]


def test_thin_buckets_are_excluded_from_the_verdict_but_stay_in_the_report():
    """The top buckets are always thinnest; they must not speak from four rows."""
    rows = [
        f.setup_row(detected_at=f.BASE, argus_score=50.0, relative_return=0.02) for _ in range(25)
    ]
    rows += [
        f.setup_row(detected_at=f.BASE, argus_score=95.0, relative_return=0.90) for _ in range(4)
    ]
    analysis = analyse_monotonicity(f.frame(rows))

    assert analysis.reportable_buckets == 1
    assert analysis.monotonic is None
    assert "floor" in analysis.undetermined_reason
    # The thin bucket is still listed — its thinness is itself a finding.
    labels = {bucket.label: bucket.metrics.sample_size for bucket in analysis.buckets}
    assert labels["90-100"] == 4


def test_an_empty_population_is_undetermined_and_says_why():
    """The answer ARGUS gives today, and it should read as expected."""
    analysis = analyse_monotonicity(f.frame([]))

    assert analysis.monotonic is None
    assert "until a real historical scan has run" in analysis.undetermined_reason
    assert len(analysis.buckets) == 6


def test_a_perfect_score_lands_in_the_top_bucket_rather_than_outside_every_one():
    """100 is the score most worth seeing; a half-open top band would drop it."""
    rows = [
        f.setup_row(detected_at=f.BASE, argus_score=100.0, relative_return=0.4) for _ in range(25)
    ]
    buckets = {bucket.label: bucket.metrics.sample_size for bucket in build_buckets(f.frame(rows))}

    assert buckets["90-100"] == 25


def test_unqualified_setups_are_not_bucketed_because_they_have_no_score():
    rows = [f.setup_row(argus_score=None) for _ in range(30)]
    buckets = build_buckets(f.frame(rows))

    assert sum(bucket.metrics.sample_size for bucket in buckets) == 0


def test_bucket_edges_are_configuration_and_changing_them_changes_the_report():
    coarse = EvaluationConfig(buckets=ScoreBucketing(edges=(0.0, 50.0, 100.0)))
    buckets = build_buckets(f.frame(f.monotone_population()), coarse)

    assert [bucket.label for bucket in buckets] == ["0-50", "50-100"]
    assert coarse.content_checksum() != EvaluationConfig().content_checksum()


def test_the_calibration_curve_states_that_argus_publishes_no_probability():
    """Module 13 stores probability as NULL, deliberately.

    Calling this "calibration" without that caveat would imply ARGUS makes
    a probabilistic claim it explicitly refuses to make.
    """
    curve = calibration_curve(f.frame(f.monotone_population()))

    assert "no probability" in curve.interpretation
    assert "NULL" in curve.interpretation


def test_calibration_bins_below_their_floor_report_a_count_and_no_rate():
    rows = [
        f.setup_row(detected_at=f.BASE, argus_score=95.0, relative_return=0.4) for _ in range(3)
    ]
    curve = calibration_curve(f.frame(rows))
    top = [entry for entry in curve.bins if entry.low == 90.0][0]

    assert top.count == 3
    assert top.observed_success_rate is None


def test_the_observed_success_rate_rises_with_score_on_a_monotone_population():
    curve = calibration_curve(f.frame(f.monotone_population()))
    reported = [entry for entry in curve.bins if entry.observed_success_rate is not None]

    assert len(reported) >= 2
    rates = [entry.observed_success_rate for entry in reported]
    assert rates == sorted(rates)
