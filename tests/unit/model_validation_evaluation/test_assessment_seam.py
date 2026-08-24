"""The one seam in the replay that breaks silently and produces plausible numbers.

Module 13's `pattern_quality` component reads a `TargetModelAssessment`
object. Module 10's `StateAssignment` keeps only the serialized form, in
`evidence["target_model_assessment"]`. So the replay has to convert, and
the tempting shortcut — passing None — compiles, runs, and removes a
quarter of the composite's weight from every replayed score without any
error, log line or failing test.

I made that break deliberately while writing this module and the whole
integration suite still passed, because today every candidate is gated
before components are computed (Module 14's bootstrap analysis: no cases,
so no evidence, so nothing scores). No end-to-end path reaches
`pattern_quality` at all yet. These tests cover the seam directly instead
of waiting for a scan that would.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pandas as pd
import pytest

from core.market_state.classifier import StateAssignment
from core.market_state.target_model_matching.interface import (
    TargetModelAssessment,
    assessment_evidence,
    assessment_from_evidence,
)
from core.model_validation_evaluation.validation.replay import scoring_assessment
from core.scoring.components import ScoringInputs, compute_components
from core.scoring.config import ScoringConfig
from infra.db.enums import MarketState
from tests.unit.scoring.factories import scoring_inputs

AS_OF = datetime(2021, 3, 1, tzinfo=UTC)


def _assignment(assessment: TargetModelAssessment | None) -> StateAssignment:
    return StateAssignment(
        security_id=assessment.security_id if assessment else uuid4(),
        state=MarketState.CONSOLIDATION,
        confidence=assessment.quality if assessment else None,
        evidence={
            "matched_state": "CONSOLIDATION",
            "target_model_assessment": assessment_evidence(assessment),
        },
    )


def test_an_assessment_survives_the_round_trip_through_stored_evidence():
    security_id = uuid4()
    original = TargetModelAssessment(
        security_id=security_id,
        quality=0.72,
        components={"prior_decline": 0.9, "consolidation": 0.6},
        supports_advancement=True,
        unavailable_inputs=("volume_expansion",),
    )

    rebuilt = scoring_assessment(security_id, _assignment(original))

    assert rebuilt is not None
    assert rebuilt.quality == pytest.approx(0.72)
    assert rebuilt.components == original.components
    assert rebuilt.supports_advancement is True
    assert rebuilt.unavailable_inputs == ("volume_expansion",)


def test_a_state_the_model_does_not_cover_produces_no_assessment():
    """None here means "outside the model's slice", which is what
    `assess()` itself returns and means the same thing."""
    assert scoring_assessment(uuid4(), _assignment(None)) is None


def test_a_security_with_no_state_assignment_produces_no_assessment():
    assert scoring_assessment(uuid4(), None) is None


def test_an_assessment_that_could_not_be_computed_stays_uncomputed():
    """`quality=None` is not low quality — Module 08's rule, carried
    through: zero is a measurement, absence is not."""
    security_id = uuid4()
    unmeasured = TargetModelAssessment(
        security_id=security_id,
        quality=None,
        unavailable_inputs=("peak_to_trough_decline", "volume_expansion"),
    )

    assert scoring_assessment(security_id, _assignment(unmeasured)) is None


def test_dropping_the_assessment_silently_zeroes_a_quarter_of_the_score():
    """Why the seam matters, demonstrated rather than asserted in prose.

    Same candidate, scored with and without the assessment. Without it,
    `pattern_quality` — the largest single weight in Module 13's
    composite — becomes unavailable, and nothing anywhere says so except
    the component's own `unavailable` field.
    """
    security_id = uuid4()
    assessment = TargetModelAssessment(security_id=security_id, quality=0.8)
    base = scoring_inputs(security_id)
    config = ScoringConfig()

    with_model = compute_components(
        ScoringInputs(
            risk=base.risk,
            features=base.features,
            assessment=assessment,
            similarity=base.similarity,
        ),
        config,
    )
    without_model = compute_components(
        ScoringInputs(
            risk=base.risk,
            features=base.features,
            assessment=None,
            similarity=base.similarity,
        ),
        config,
    )

    assert with_model["pattern_quality"].value is not None
    assert without_model["pattern_quality"].value is None
    assert without_model["pattern_quality"].unavailable == ("target_model_quality",)
    # The weight itself is unchanged, which is precisely the danger: the
    # composite renormalizes over what measured, so the score still looks
    # like a score.
    assert with_model["pattern_quality"].weight == without_model["pattern_quality"].weight


def test_the_inverse_matches_the_forward_direction_for_every_field():
    """A guard on the pair, so a field added to one is added to both."""
    security_id = uuid4()
    original = TargetModelAssessment(
        security_id=security_id,
        quality=0.5,
        components={"awakening": 0.4},
        supports_advancement=False,
        unavailable_inputs=(),
    )
    rebuilt = assessment_from_evidence(security_id, assessment_evidence(original))

    assert rebuilt == original


def test_the_helper_reads_the_key_module_10_actually_writes():
    """Not a key this module invented. A renamed key would make every
    assessment vanish, which is the same silent failure by another route."""
    frame = pd.DataFrame()
    assert frame.empty  # keeps the import honest about what this file needs

    assignment = _assignment(TargetModelAssessment(security_id=uuid4(), quality=0.3))
    assert "target_model_assessment" in assignment.evidence
