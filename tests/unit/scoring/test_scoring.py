"""Scoring, gating, and the two numbers that must be able to disagree."""

from __future__ import annotations

import pytest

from core.historical_similarity.statistics import SampleSufficiency
from core.risk_context.events import EventCoverage
from core.risk_context.invalidation import EligibilityTrend
from core.scoring.components import (
    FUNDAMENTAL_CONTEXT,
    HISTORICAL_EVIDENCE,
    PATTERN_QUALITY,
    compute_components,
)
from core.scoring.config import ScoringConfig
from core.scoring.engine import score_candidate
from core.scoring.gating import ScoringDecision, weight_coverage
from tests.unit.scoring.factories import (
    AS_OF,
    HEALTHY_FEATURES,
    adequate,
    insufficient,
    lineage,
    scoring_inputs,
    sparse,
    statistics,
)

STRONG_FEATURES = {
    **HEALTHY_FEATURES,
    "market_regime_trend": 0.0010,
    "market_regime_volatility": 0.10,
    "market_regime_drawdown": 0.0,
    "avg_dollar_volume": 5_000_000.0,
    "spread_proxy": 0.01,
    "volatility_compression": 0.50,
    "atr_percentile": 0.10,
    "normalized_range_width": 0.05,
}


def _score(**kwargs):
    return score_candidate(scoring_inputs(**kwargs), as_of=AS_OF, lineage=lineage())


# --------------------------------------------------------------------------
# The expected current state of the world
# --------------------------------------------------------------------------


def test_the_current_expected_outcome_is_mostly_insufficient_evidence():
    """Asserted as correct, not worked around.

    Until Module 17's scan populates the case dataset, Module 11 returns
    INSUFFICIENT for essentially every candidate. That removes the
    historical-evidence component, which alone carries more weight than
    the coverage floor allows to go missing. A run that produced confident
    scores today would mean something had gone wrong, not right.
    """
    candidates = [scoring_inputs() for _ in range(20)]
    results = [score_candidate(c, as_of=AS_OF, lineage=lineage()) for c in candidates]

    assert all(r.decision is ScoringDecision.INSUFFICIENT_EVIDENCE for r in results)
    assert all(r.argus_score is None for r in results)
    assert all("weight" in r.verdict.reason for r in results)


def test_losing_only_the_historical_component_is_enough_to_refuse():
    """The specific arithmetic behind the statement above."""
    config = ScoringConfig()
    result = _score(cross=insufficient())

    assert result.components[HISTORICAL_EVIDENCE].value is None
    assert result.weight_coverage < config.thresholds.min_weight_coverage.value
    assert result.decision is ScoringDecision.INSUFFICIENT_EVIDENCE
    # And the rest of the evidence was fine — this is not a broken candidate.
    assert result.components[PATTERN_QUALITY].value is not None


def test_an_insufficient_sample_is_not_scored_as_a_bad_one():
    """It has no value at all, which is a different thing from a low one."""
    component = compute_components(scoring_inputs(cross=insufficient()), ScoringConfig())[
        HISTORICAL_EVIDENCE
    ]

    assert component.value is None
    assert "INSUFFICIENT" in component.note


# --------------------------------------------------------------------------
# Confidence is not a restatement of the score
# --------------------------------------------------------------------------


def test_a_high_score_on_thin_evidence_is_structurally_possible():
    """The case the Module 13 brief asks to see in the test suite.

    Everything structural about this candidate is excellent and the
    pattern match is perfect — so `argus_score` is high, correctly. The
    historical evidence behind it is six cases with an interval a third of
    the range wide, the feature window is only 60% filled, and earnings
    are two days out. `confidence` collapses. Both numbers are right, and
    a reader who saw only the first would be misled.
    """
    result = _score(
        quality=1.0,
        cross=sparse(0.05),
        feature_values=STRONG_FEATURES,
        feature_coverage=0.6,
        liquidity=0.0,
        expansion=1.0,
        days_to_event=2.0,
    )

    assert result.decision is ScoringDecision.SCORED
    assert result.argus_score > 80.0
    assert result.confidence < 40.0
    assert result.argus_score - result.confidence > 40.0


def test_identical_scores_from_different_evidence_quality_differ_in_confidence():
    """Two candidates, the same `argus_score` to the last decimal.

    Both failure-rate intervals top out at 0.42, so the historical
    component — which is scored from the pessimistic bound — is identical,
    and every other input matches. What differs is 200 cases against 6,
    and an interval 0.12 wide against one 0.32 wide.
    """
    well_supported = statistics(
        count=200,
        sufficiency=SampleSufficiency.ADEQUATE,
        failure_rate=0.36,
        interval=(0.30, 0.42),
    )
    thin = statistics(
        count=6,
        sufficiency=SampleSufficiency.SPARSE,
        failure_rate=0.26,
        interval=(0.10, 0.42),
    )

    first = _score(cross=well_supported)
    second = _score(cross=thin)

    assert first.argus_score == pytest.approx(second.argus_score)
    assert first.confidence > second.confidence
    assert first.confidence - second.confidence > 20.0


def test_confidence_reads_no_component_and_no_pattern_quality():
    """Moving the pattern match from floor to ceiling must not move
    confidence at all, or the two numbers are not independent."""
    weak = _score(quality=0.05, cross=adequate())
    strong = _score(quality=0.99, cross=adequate())

    assert strong.argus_score > weak.argus_score
    assert strong.confidence == pytest.approx(weak.confidence)


def test_an_imminent_binary_event_lowers_confidence_not_just_risk():
    """Module 12's finding #4, implemented.

    An earnings release three days out does not make the base worse — it
    makes the thesis less structurally determined, which is a statement
    about how much the reading is worth.
    """
    distant = _score(cross=adequate(), days_to_event=80.0)
    imminent = _score(cross=adequate(), days_to_event=3.0)

    assert imminent.confidence < distant.confidence
    assert imminent.risk_score > distant.risk_score


def test_unknown_calendar_coverage_does_not_earn_full_event_clarity():
    """Ignorance is not clarity. The factor drops out instead."""
    from core.scoring.confidence import EVENT_CLARITY

    unknown = _score(cross=adequate(), event_coverage=EventCoverage.UNAVAILABLE)

    # The whole candidate is refused, because Module 12 reports the event
    # flag as undetermined and therefore no risk_score exists.
    assert unknown.decision is ScoringDecision.INSUFFICIENT_EVIDENCE
    assert unknown.confidence_assessment.factors[EVENT_CLARITY].value is None


def test_no_scheduled_event_is_full_clarity_because_it_is_a_real_negative():
    from core.scoring.confidence import EVENT_CLARITY

    quiet = _score(
        cross=adequate(), event_coverage=EventCoverage.NONE_SCHEDULED, days_to_event=None
    )

    assert quiet.decision is ScoringDecision.SCORED
    assert quiet.confidence_assessment.factors[EVENT_CLARITY].value == pytest.approx(100.0)


# --------------------------------------------------------------------------
# The interval travels with the number
# --------------------------------------------------------------------------


def test_the_same_failure_rate_scores_worse_when_the_sample_is_thin():
    """Binding requirement #6, in one assertion.

    A 0.30 failure rate from 200 cases and from 6 cases are the same point
    estimate. The component is computed from the interval's upper bound,
    so the thin one scores materially worse — the interval is part of the
    input, not metadata to discard.
    """
    strong = compute_components(scoring_inputs(cross=adequate(0.30)), ScoringConfig())
    thin = compute_components(scoring_inputs(cross=sparse(0.30)), ScoringConfig())

    assert strong[HISTORICAL_EVIDENCE].inputs["failure_rate"] == pytest.approx(
        thin[HISTORICAL_EVIDENCE].inputs["failure_rate"]
    )
    assert strong[HISTORICAL_EVIDENCE].value > thin[HISTORICAL_EVIDENCE].value


def test_the_component_records_the_bound_it_actually_used():
    """A stored score must be re-derivable, including which number it read."""
    component = compute_components(scoring_inputs(cross=sparse(0.30)), ScoringConfig())[
        HISTORICAL_EVIDENCE
    ]

    assert component.inputs["failure_rate_upper_bound"] > component.inputs["failure_rate"]
    assert "upper bound" in component.note


def test_same_asset_evidence_never_enters_the_cross_asset_component():
    """Module 11's structural separation, preserved.

    A security's own three prior attempts are its own history, not a
    cross-sectional claim about the pattern. Loading the same-asset scope
    with spectacular statistics must move nothing.
    """
    from dataclasses import replace

    from tests.unit.scoring.factories import similarity

    inputs = scoring_inputs(cross=adequate(0.30))
    baseline = compute_components(inputs, ScoringConfig())[HISTORICAL_EVIDENCE].value

    loaded = replace(
        inputs,
        similarity=similarity(
            inputs.risk.security_id, adequate(0.30), same=adequate(0.0, count=500)
        ),
    )

    assert compute_components(loaded, ScoringConfig())[HISTORICAL_EVIDENCE].value == pytest.approx(
        baseline
    )


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def test_lost_eligibility_is_gated_out_entirely_not_scored_with_a_penalty():
    """Module 12's finding #3.

    A broken thesis is not a magnitude. If it were folded into
    `risk_score` as one input among several, a strong pattern match could
    numerically outweigh it — which is exactly the outcome the gate exists
    to make impossible.
    """
    result = _score(cross=adequate(), eligibility_trend=EligibilityTrend.LOST_ELIGIBILITY)

    assert result.decision is ScoringDecision.GATED_LOST_ELIGIBILITY
    assert result.writes_signal is False
    assert result.evidence_status is None
    assert (result.argus_score, result.confidence, result.risk_score) == (None, None, None)
    assert result.verdict.detail["routed_to"] == "Module 14 (lifecycle)"


def test_a_gated_candidate_is_not_merely_a_low_scoring_one():
    """The same candidate with and without the invalidation signal."""
    healthy = _score(cross=adequate())
    broken = _score(cross=adequate(), eligibility_trend=EligibilityTrend.LOST_ELIGIBILITY)

    assert healthy.decision is ScoringDecision.SCORED
    assert healthy.argus_score is not None
    assert broken.decision is ScoringDecision.GATED_LOST_ELIGIBILITY


def test_never_eligible_is_not_treated_as_lost_eligibility():
    """Module 12 keeps them apart; this module must not merge them."""
    result = _score(cross=adequate(), eligibility_trend=EligibilityTrend.NEVER_ELIGIBLE)

    assert result.decision is not ScoringDecision.GATED_LOST_ELIGIBILITY


def test_module_09s_own_outcome_gates_when_it_is_supplied():
    from uuid import uuid4

    from core.candidate_detection.eligibility.gates import EligibilityOutcome, GateResult
    from infra.db.enums import EligibilityGate

    security_id = uuid4()
    inputs = scoring_inputs(security_id, cross=adequate())
    outcome = EligibilityOutcome(
        security_id=security_id,
        as_of=AS_OF,
        results={
            gate: GateResult(gate=gate, passed=gate is not EligibilityGate.LIQUIDITY, detail={})
            for gate in EligibilityGate
        },
    )

    result = score_candidate(inputs, as_of=AS_OF, lineage=lineage(), eligibility=outcome)

    assert result.decision is ScoringDecision.GATED_INELIGIBLE
    assert result.verdict.detail["failed_gates"] == ["LIQUIDITY"]


@pytest.mark.parametrize(
    ("liquidity", "expansion", "days"),
    [(None, 1.1, 40.0), (0.1, None, 40.0), (0.1, 1.1, None)],
)
def test_any_undetermined_risk_input_refuses_the_whole_candidate(liquidity, expansion, days):
    """Binding requirement #1, at its strongest reading.

    A candidate with an unmeasured risk signal is one ARGUS knows nothing
    about. Handing it a middling `risk_score` would let it outrank a
    candidate that was actually measured and came back clean, so it gets
    no numbers at all — which is also what Module 03's CHECK constraint
    requires, since a SCORED row must carry a risk_score.
    """
    coverage = EventCoverage.UNAVAILABLE if days is None else EventCoverage.KNOWN
    result = _score(
        cross=adequate(),
        liquidity=liquidity,
        expansion=expansion,
        days_to_event=days,
        event_coverage=coverage,
    )

    assert result.decision is ScoringDecision.INSUFFICIENT_EVIDENCE
    assert result.risk_score is None
    assert result.verdict.detail.get("undetermined_risk_inputs")


def test_a_zero_weight_component_does_not_count_against_coverage():
    """`fundamental_context` is permanently absent by design. If it
    counted as unmeasured, every candidate would look partly unmeasured
    forever and the coverage floor would mean something else."""
    components = compute_components(scoring_inputs(cross=adequate()), ScoringConfig())

    assert components[FUNDAMENTAL_CONTEXT].value is None
    assert components[FUNDAMENTAL_CONTEXT].contributes is False
    assert weight_coverage(components) == pytest.approx(1.0)


def test_a_missing_feature_vector_refuses_rather_than_scoring_on_what_is_left():
    result = _score(cross=adequate(), with_features=False)

    assert result.decision is ScoringDecision.INSUFFICIENT_EVIDENCE
    assert result.weight_coverage < 1.0


# --------------------------------------------------------------------------
# argus_score is not a probability
# --------------------------------------------------------------------------


def test_probability_is_never_populated_and_its_definition_always_is():
    """The structure exists; the number does not, and will not until a
    model is calibrated against real outcomes after Module 17."""
    from core.scoring.config import PROBABILITY_DEFINITION, PROBABILITY_STATUS

    result = _score(cross=adequate())

    assert result.decision is ScoringDecision.SCORED
    assert result.argus_score is not None
    assert result.probability is None
    assert result.probability_definition == PROBABILITY_DEFINITION
    assert "NOT_YET_CALIBRATED" in result.probability_status
    assert PROBABILITY_STATUS in result.as_dict()["probability_status"]


def test_the_score_and_the_probability_are_never_the_same_field():
    """A name-level check: nothing in the serialized result lets a reader
    mistake one for the other, and the two never carry the same value."""
    payload = _score(cross=adequate()).as_dict()

    assert payload["argus_score"] is not None
    assert payload["probability"] is None
    assert payload["argus_score"] != payload["probability"]
    assert "probability" not in payload["components"]
    assert "never a probability" in payload["note"]


def test_the_five_numbers_are_computed_from_different_evidence():
    """None of them is a rescaling of another.

    Moving the pattern match moves only `argus_score`; moving the sample
    quality moves only `confidence`; moving a risk flag moves `risk_score`
    and, through the risk component, `argus_score` — but never
    `opportunity_score`, which comes from the MFE distribution alone.
    """
    baseline = _score(cross=adequate())
    riskier = _score(cross=adequate(), liquidity=0.95)

    assert riskier.risk_score > baseline.risk_score
    assert riskier.argus_score < baseline.argus_score
    assert riskier.opportunity_score == pytest.approx(baseline.opportunity_score)
    assert riskier.confidence == pytest.approx(baseline.confidence)


def test_the_composite_renormalizes_over_the_components_that_measured():
    """Reachable only under a lowered coverage floor — deliberately tested
    anyway.

    Under the default weights no single component's absence leaves
    coverage above 0.85, so a partially-measured composite never occurs
    today and this arithmetic is never exercised by a normal run. It
    exists for the reweighting Module 17 will bring, and untested
    arithmetic that only wakes up after a recalibration is exactly the
    kind that is wrong when it does.

    Without renormalizing, the score would be scaled down by the missing
    weight — a candidate would look worse for an absence rather than
    simply being judged on less.
    """
    from core.scoring.config import CALIBRATABLE, ComponentWeight, ScoringThresholds

    lenient = ScoringConfig(
        thresholds=ScoringThresholds(
            min_weight_coverage=ComponentWeight(
                value=0.70, kind=CALIBRATABLE, rationale="test override"
            )
        )
    )
    # Analogues exist and produced an MFE distribution, but none of them
    # resolved into a success or failure — so there is no failure rate to
    # score the historical component from, while opportunity survives.
    unresolved = statistics(count=200, sufficiency=SampleSufficiency.ADEQUATE, failure_rate=None)
    result = score_candidate(
        scoring_inputs(cross=unresolved), as_of=AS_OF, lineage=lineage(), config=lenient
    )

    assert result.decision is ScoringDecision.SCORED
    assert result.weight_coverage == pytest.approx(1.0 - lenient.weights.historical_evidence.value)

    weights = lenient.weights.as_dict()
    measured = [
        (weights[name], result.component_value(name))
        for name in weights
        if weights[name] > 0 and result.component_value(name) is not None
    ]
    expected = sum(w * v for w, v in measured) / sum(w for w, _ in measured)

    assert result.argus_score == pytest.approx(expected)
    # And the un-renormalized sum would have been materially lower.
    assert result.argus_score > sum(w * v for w, v in measured)
