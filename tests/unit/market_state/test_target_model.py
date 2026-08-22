"""target-model-v1's containment: does the boundary actually hold?

The brief is explicit that the model is *not a peer* to the state machine
— it is contained by it, judging pattern quality within four states rather
than assigning states. The architecture anticipates a second model one day
without building support for one now.

So the tests here are mostly structural: they check that the engine could
run against a different model without changing, and that this model cannot
reach outside its four states. The quality arithmetic itself is checked
for direction only, for the same reason the state sequence tests are —
none of the weights has been validated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pandas as pd
import pytest

from core.market_state import classify_states
from core.market_state.target_model_matching.interface import (
    TargetModel,
    TargetModelAssessment,
    assessment_evidence,
)
from core.market_state.target_model_matching.models.target_model_v1 import (
    COVERED_STATES,
    MODEL_INPUTS,
    TargetModelV1,
    TargetModelV1Thresholds,
)
from infra.db.enums import MarketState
from tests.unit.feature_engine.lifecycle import LIFECYCLE_ID
from tests.unit.market_state.lifecycle import PHASE_ORDER, phase_vectors

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)


@dataclass
class StubModel:
    """A hypothetical second target model, sharing no code with v1."""

    name: str = "stub-model"
    fixed_quality: float = 0.9

    def covered_states(self) -> frozenset[MarketState]:
        return frozenset({MarketState.DOWN_TREND})

    def required_features(self) -> tuple[str, ...]:
        return ("drawdown_pct",)

    def assess(self, features, states, as_of) -> dict[UUID, TargetModelAssessment]:
        return {
            security_id: TargetModelAssessment(
                security_id=security_id,
                quality=self.fixed_quality,
                components={"stub": self.fixed_quality},
                supports_advancement=True,
            )
            for security_id in states[states.isin(self.covered_states())].index
        }


# --------------------------------------------------------------------------
# Containment
# --------------------------------------------------------------------------


def test_the_model_satisfies_the_protocol_without_inheriting_from_it():
    """Structural typing, so a second model owes the engine no import."""
    assert isinstance(TargetModelV1(), TargetModel)
    assert isinstance(StubModel(), TargetModel)


def test_the_engine_runs_against_a_different_model_unchanged():
    """The containment claim, exercised.

    Swapping the model changes which securities carry confidence and
    nothing else. If the engine had to know anything about v1, this could
    not work — and the "second model someday" the architecture anticipates
    would be a rewrite rather than a substitution.
    """
    vectors = phase_vectors()["decline"]

    with_v1 = classify_states(vectors).assignments[LIFECYCLE_ID]
    with_stub = classify_states(vectors, target_model=StubModel()).assignments[LIFECYCLE_ID]

    # Same state — the model does not assign states.
    assert with_v1.state is with_stub.state is MarketState.DOWN_TREND
    # v1 does not cover DOWN_TREND, so it offers no confidence; the stub does.
    assert with_v1.confidence is None
    assert with_stub.confidence == pytest.approx(0.9)


def test_the_model_only_assesses_the_states_it_covers():
    """It cannot silently opine outside its remit."""
    model = TargetModelV1()
    assert {
        MarketState.CONSOLIDATION,
        MarketState.ACCUMULATION,
        MarketState.BREAKOUT_WATCH,
        MarketState.BREAKOUT_READY,
    } == COVERED_STATES

    states = pd.Series(
        {uuid4(): MarketState.DOWN_TREND, uuid4(): MarketState.UPTREND}, dtype=object
    )
    frame = pd.DataFrame({name: [0.5, 0.5] for name in MODEL_INPUTS}, index=states.index)

    assert model.assess(frame, states, AS_OF) == {}


def test_states_outside_the_model_carry_no_confidence():
    """An unassessed state has *no* confidence, not low confidence.

    Defaulting to 0.0 would make a security the model never looked at
    indistinguishable from one it judged a poor match — the same
    None-versus-zero distinction Module 08 established for features.
    """
    vectors = phase_vectors()
    for phase in PHASE_ORDER:
        assignment = classify_states(vectors[phase]).assignments[LIFECYCLE_ID]
        if assignment.state not in COVERED_STATES:
            assert assignment.confidence is None


def test_the_engine_asks_the_model_which_features_it_needs():
    """The engine must not hardcode the model's inputs.

    Found while building: the classifier originally built its frame from
    the state predicates' features alone, and the model crashed on a
    missing column. Asking via `required_features()` is what keeps the
    engine ignorant of the model's internals.
    """
    engine_only = set()
    from core.market_state.states import CLASSIFICATION_FEATURES

    engine_only.update(CLASSIFICATION_FEATURES)

    assert set(MODEL_INPUTS) - engine_only, (
        "fixture premise: the model must read at least one feature the "
        "predicates do not, or this test proves nothing"
    )
    # And classification succeeds anyway, because the frame is the union.
    assignment = classify_states(phase_vectors()["consolidation"]).assignments[LIFECYCLE_ID]
    assert assignment.confidence is not None


# --------------------------------------------------------------------------
# The quality score — direction only
# --------------------------------------------------------------------------


def test_quality_is_bounded():
    vectors = phase_vectors()
    for phase in PHASE_ORDER:
        assignment = classify_states(vectors[phase]).assignments[LIFECYCLE_ID]
        if assignment.confidence is not None:
            assert 0.0 <= assignment.confidence <= 1.0


def test_quality_is_none_when_too_few_inputs_are_present():
    """A score built on absences would be worse than no score."""
    model = TargetModelV1()
    security_id = uuid4()
    states = pd.Series({security_id: MarketState.CONSOLIDATION}, dtype=object)
    frame = pd.DataFrame({name: [float("nan")] for name in MODEL_INPUTS}, index=[security_id])

    assessment = model.assess(frame, states, AS_OF)[security_id]
    assert assessment.quality is None
    assert not assessment.assessed
    assert set(assessment.unavailable_inputs) == set(MODEL_INPUTS)


def test_a_deeper_decline_scores_at_least_as_well():
    """Direction, not magnitude — the only claim the weights support."""
    model = TargetModelV1()
    shallow, deep = uuid4(), uuid4()
    states = pd.Series(
        {shallow: MarketState.CONSOLIDATION, deep: MarketState.CONSOLIDATION}, dtype=object
    )
    frame = pd.DataFrame({name: [0.5, 0.5] for name in MODEL_INPUTS}, index=[shallow, deep])
    frame.loc[shallow, "peak_to_trough_decline"] = -0.10
    frame.loc[deep, "peak_to_trough_decline"] = -0.55

    assessed = model.assess(frame, states, AS_OF)
    assert assessed[deep].quality >= assessed[shallow].quality


def test_the_components_are_named_so_a_match_is_explainable():
    """A bare number would make the score unauditable."""
    assignment = classify_states(phase_vectors()["consolidation"]).assignments[LIFECYCLE_ID]
    components = assignment.evidence["target_model_assessment"]["components"]

    assert set(components) <= {"prior_decline", "stabilization", "consolidation", "awakening"}
    assert components


def test_equal_weights_are_the_default_because_nobody_knows_better():
    """An honesty check on the prior.

    Unequal weights would imply someone knows which phase matters more.
    Nobody does yet, and a future edit that quietly tilted them should be
    a deliberate, visible act.
    """
    weights = TargetModelV1Thresholds().weights()
    assert len(set(weights.values())) == 1
    assert sum(weights.values()) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# The unresolved boundary, recorded rather than acted on
# --------------------------------------------------------------------------


def test_supports_advancement_is_recorded_but_does_not_gate_the_state():
    """The flagged ambiguity, pinned as behaviour.

    The brief describes the model both as "judging transitions" and as
    explicitly *not* assigning states. This module implements the second
    reading and records the first as evidence, so switching later is a
    one-place change rather than a redesign.

    If a future edit made `supports_advancement` gate transitions, this
    test fails — which is the intended alarm, not a nuisance.
    """
    vectors = phase_vectors()["consolidation"]

    permissive = classify_states(vectors).assignments[LIFECYCLE_ID]
    strict = classify_states(
        vectors,
        target_model=TargetModelV1(
            TargetModelV1Thresholds(advancement_quality=1.0)  # never supports advancing
        ),
    ).assignments[LIFECYCLE_ID]

    assert permissive.state is strict.state, (
        "the target model must not change which state is assigned"
    )
    assert strict.evidence["target_model_assessment"]["supports_advancement"] is False


def test_the_assessment_serializes_for_the_transition_evidence():
    """Whatever the model says has to survive into storage."""
    assessment = TargetModelAssessment(
        security_id=uuid4(), quality=0.7, components={"a": 0.7}, supports_advancement=True
    )
    evidence = assessment_evidence(assessment)

    assert evidence["assessed"] is True
    assert evidence["quality"] == pytest.approx(0.7)
    assert evidence["supports_advancement"] is True

    absent = assessment_evidence(None)
    assert absent["assessed"] is False
    assert absent["reason"] == "state_not_covered_by_target_model"
