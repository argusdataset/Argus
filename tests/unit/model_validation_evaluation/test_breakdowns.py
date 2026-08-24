"""Regime and evidence-scope splits, and the sample floors that keep them honest.

Splitting a population makes every part smaller, which is exactly when a
floor stops being a formality. These tests are mostly about what the
breakdowns *decline* to say.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.model_validation_evaluation.evaluation.breakdowns import (
    CROSS_ASSET_ONLY,
    SAME_ASSET_SUPPORTED,
    by_evidence_scope,
    by_regime,
    by_regime_and_bucket,
    sufficiency_note,
)
from core.model_validation_evaluation.evaluation.config import (
    EvaluationConfig,
    EvaluationThreshold,
    EvaluationThresholds,
)
from infra.db.enums import MarketState, OutcomeStatus
from tests.unit.model_validation_evaluation import factories as f


def low_floor(minimum: float = 3.0) -> EvaluationConfig:
    return EvaluationConfig(
        thresholds=EvaluationThresholds(
            min_bucket_sample=EvaluationThreshold(
                value=minimum, kind="calibratable", rationale="test"
            ),
            min_regime_sample=EvaluationThreshold(
                value=minimum, kind="calibratable", rationale="test"
            ),
        )
    )


def test_regimes_are_split_by_where_the_outcome_was_realized():
    rows = [f.setup_row(argus_score=80.0, regime=MarketState.UPTREND) for _ in range(4)] + [
        f.setup_row(argus_score=80.0, regime=MarketState.DOWN_TREND) for _ in range(4)
    ]
    breakdown = by_regime(f.frame(rows), low_floor())

    assert set(breakdown.groups) == {"UPTREND", "DOWN_TREND"}
    assert breakdown.groups["UPTREND"].sample_size == 4


def test_a_thin_regime_reports_a_count_and_never_a_rate():
    rows = [f.setup_row(argus_score=80.0, regime=MarketState.UPTREND) for _ in range(30)]
    rows += [f.setup_row(argus_score=80.0, regime=MarketState.DOWN_TREND)]
    breakdown = by_regime(f.frame(rows), EvaluationConfig())

    assert "UPTREND" in breakdown.reportable()
    assert breakdown.thin() == {"DOWN_TREND": 1}
    assert breakdown.groups["DOWN_TREND"].precision is None


def test_a_regime_ARGUS_never_recorded_is_labelled_rather_than_dropped():
    """A NULL regime is a fact about the outcome, not a row to discard."""
    rows = [f.setup_row(argus_score=80.0) for _ in range(3)]
    frame = f.frame(rows)
    frame["market_regime_at_outcome"] = None
    breakdown = by_regime(frame, low_floor())

    assert set(breakdown.groups) == {"UNRECORDED"}


def test_a_security_with_prior_concluded_setups_is_same_asset_supported():
    """Module 11 refuses to blend the two evidence kinds; so does this."""
    rows = f.repeated_security(4) + [f.setup_row(argus_score=70.0) for _ in range(3)]
    breakdown = by_evidence_scope(f.frame(rows), low_floor())

    # Three of the four repeats have a predecessor; the first does not,
    # and joins the three genuinely-new securities.
    assert breakdown.groups[SAME_ASSET_SUPPORTED].sample_size == 3
    assert breakdown.groups[CROSS_ASSET_ONLY].sample_size == 4


def test_a_securitys_first_setup_is_never_same_asset_supported():
    """The first cycle a security completes cannot have been informed by
    its own earlier ones — there were none."""
    rows = [f.setup_row(argus_score=70.0) for _ in range(5)]
    breakdown = by_evidence_scope(f.frame(rows), low_floor())

    assert SAME_ASSET_SUPPORTED not in breakdown.groups
    assert breakdown.groups[CROSS_ASSET_ONLY].sample_size == 5


def test_the_scope_split_uses_detection_order_not_row_order():
    """Rows arriving newest-first must not make the newest look like the first.

    The counts alone cannot catch this — two of three rows are
    same-asset-supported whichever end you count from — so the assertion
    is about *which* setup is the cross-asset-only one. It has to be the
    earliest-detected, in both orderings. An earlier version of this test
    compared only the sizes and passed against a deliberately broken
    implementation; this one does not.
    """
    rows = f.repeated_security(3)
    earliest = min(rows, key=lambda row: row["detected_at"])["setup_id"]

    for ordering in (rows, list(reversed(rows))):
        frame = f.frame(ordering)
        scope = _scope_series(frame)

        assert scope.loc[frame["setup_id"] == earliest].tolist() == [CROSS_ASSET_ONLY]
        assert sorted(scope.tolist()) == sorted(
            [CROSS_ASSET_ONLY, SAME_ASSET_SUPPORTED, SAME_ASSET_SUPPORTED]
        )


def _scope_series(frame):
    from core.model_validation_evaluation.evaluation.breakdowns import _evidence_scope

    return _evidence_scope(frame)


def test_the_regime_by_bucket_crosstab_exists_even_when_every_cell_is_thin():
    """Almost every cell will be INSUFFICIENT at realistic scale. The
    structure is what a real run fills in; an empty dict would leave
    nowhere for it to go."""
    crosstab = by_regime_and_bucket(f.frame(f.mixed_population()))

    assert set(crosstab) >= {"UPTREND", "DOWN_TREND"}
    assert "60-70" in crosstab["UPTREND"]


def test_the_sufficiency_note_says_plainly_when_nothing_can_be_claimed():
    rows = [f.setup_row(argus_score=80.0, regime=MarketState.UPTREND) for _ in range(4)]
    note = sufficiency_note(by_regime(f.frame(rows), EvaluationConfig()))

    assert "Nothing regime-specific can be claimed" in note


def test_the_sufficiency_note_counts_both_halves_when_some_groups_report():
    rows = [f.setup_row(argus_score=80.0, regime=MarketState.UPTREND) for _ in range(30)]
    rows += [f.setup_row(argus_score=80.0, regime=MarketState.DOWN_TREND)]
    note = sufficiency_note(by_regime(f.frame(rows), EvaluationConfig()))

    assert "1 of 2" in note
    assert "1 reported a count only" in note


def test_an_empty_population_produces_an_empty_breakdown_and_says_so():
    breakdown = by_regime(f.frame([]), EvaluationConfig())

    assert breakdown.groups == {}
    assert "No market_regime_at_outcome groups" in sufficiency_note(breakdown)


def test_regime_metrics_are_computed_from_that_regimes_rows_only():
    """The obvious property, asserted because a mis-keyed mask would give
    every regime the whole population's numbers and look plausible."""
    winners = [
        f.setup_row(
            argus_score=80.0,
            regime=MarketState.UPTREND,
            relative_return=0.30,
            outcome_status=OutcomeStatus.SUCCESS,
            detected_at=f.BASE + timedelta(days=n),
        )
        for n in range(5)
    ]
    losers = [
        f.setup_row(
            argus_score=80.0,
            regime=MarketState.DOWN_TREND,
            relative_return=-0.20,
            outcome_status=OutcomeStatus.FAILED,
            detected_at=f.BASE + timedelta(days=100 + n),
        )
        for n in range(5)
    ]
    breakdown = by_regime(f.frame(winners + losers), low_floor())

    assert breakdown.groups["UPTREND"].expectancy == pytest.approx(0.30)
    assert breakdown.groups["DOWN_TREND"].expectancy == pytest.approx(-0.20)
    assert breakdown.groups["UPTREND"].precision == 1.0
    assert breakdown.groups["DOWN_TREND"].precision == 0.0
