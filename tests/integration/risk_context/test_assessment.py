"""The whole assessment, assembled: what it says and what it refuses to say."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from core.data_validation.result import MissReason
from core.feature_engine.vector import FeatureEvidence, FeatureVector
from core.market_state import (
    MarketStateConfig,
    publish_target_model_version,
    record_transitions,
)
from core.market_state.classifier import ClassificationResult, StateAssignment
from core.risk_context.assessment import assess_risk_context
from core.risk_context.config import RiskConfig
from core.risk_context.events import EventCoverage
from core.risk_context.flags import EVENT_PROXIMITY, LIQUIDITY_DEGREE, VOLATILITY_SPIKE
from core.risk_context.invalidation import BACKWARD_TRANSITION, LOST_ELIGIBILITY
from data.canonical_model.records import CanonicalTimeframe
from infra.db.enums import EligibilityGate, MarketState

AS_OF = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


def _vector(
    security_id: UUID,
    features: dict[str, float | None],
    *,
    unavailable: tuple[str, ...] = (),
) -> FeatureVector:
    return FeatureVector(
        security_id=security_id,
        as_of=AS_OF,
        feature_schema_version_id=None,
        timeframe=CanonicalTimeframe.DAILY,
        event_time=AS_OF,
        availability_time=AS_OF,
        features=features,
        evidence=FeatureEvidence(
            bars_available=400,
            bars_required=252,
            unavailable_features=unavailable,
        ),
    )


def test_a_candidate_with_imminent_earnings_and_thin_volume_itemizes_both(
    connection, register, add_event
):
    """The shape Module 13 will read: named inputs, each with its own
    verdict, degree and re-derivable detail."""
    security_id = register("ITEM")
    add_event(
        security_id,
        scheduled_for=AS_OF + timedelta(days=5),
        available=AS_OF - timedelta(days=30),
    )

    context = assess_risk_context(
        connection,
        security_id,
        as_of=AS_OF,
        features=_vector(
            security_id,
            {
                "avg_dollar_volume": 75_000.0,
                "atr_percentile": 0.30,
                "volatility_compression": 0.80,
            },
        ),
    )

    assert set(context.raised_flags()) == {LIQUIDITY_DEGREE, EVENT_PROXIMITY}
    assert context.flag(VOLATILITY_SPIKE).raised is False
    assert context.flag(EVENT_PROXIMITY).degree == pytest.approx(5.0)
    assert context.events.coverage is EventCoverage.KNOWN
    assert context.complete is False  # no eligibility or state history recorded


def test_an_assessment_without_a_feature_vector_still_returns_and_says_what_is_missing(
    connection, register, add_event
):
    """Refusing to produce a result would hide the partial information
    that is this module's whole output — `min_inputs_for_assessment` is
    zero on purpose."""
    security_id = register("NOVEC")
    add_event(
        security_id,
        scheduled_for=AS_OF + timedelta(days=3),
        available=AS_OF - timedelta(days=10),
    )

    context = assess_risk_context(connection, security_id, as_of=AS_OF, features=None)

    assert context.flag(EVENT_PROXIMITY).raised is True
    assert set(context.undetermined_flags()) == {LIQUIDITY_DEGREE, VOLATILITY_SPIKE}
    missing = context.missing_inputs()
    assert missing[LIQUIDITY_DEGREE] is MissReason.NOT_YET_AVAILABLE
    assert missing[VOLATILITY_SPIKE] is MissReason.NOT_YET_AVAILABLE
    assert context.metadata["feature_vector_present"] is False


def test_a_security_with_no_data_at_all_reports_three_absences_not_three_all_clears(
    connection, register
):
    """The failure this module is built to avoid: a candidate about which
    ARGUS knows nothing must not read as a candidate with no risk."""
    security_id = register("EMPTY")

    context = assess_risk_context(connection, security_id, as_of=AS_OF)

    assert context.raised_flags() == ()
    assert len(context.undetermined_flags()) == 3
    assert context.events.miss_reason is MissReason.NEVER_INGESTED
    assert set(context.missing_inputs()) == {
        LIQUIDITY_DEGREE,
        VOLATILITY_SPIKE,
        EVENT_PROXIMITY,
        "eligibility_history",
        "state_history",
    }


def test_invalidation_signals_travel_with_the_flags(connection, register, record_eligibility):
    security_id = register("INVAL")
    version_id = publish_target_model_version(connection, MarketStateConfig())
    for step, state in enumerate([MarketState.BREAKOUT_READY, MarketState.CONSOLIDATION]):
        record_transitions(
            connection,
            ClassificationResult(
                as_of=AS_OF - timedelta(weeks=4 - step),
                target_model_version_id=version_id,
                assignments={
                    security_id: StateAssignment(
                        security_id=security_id,
                        state=state,
                        confidence=None,
                        evidence={"matched_state": state.value},
                    )
                },
            ),
        )
    record_eligibility(security_id, evaluated_at=AS_OF - timedelta(days=90))
    record_eligibility(security_id, evaluated_at=AS_OF, failed=(EligibilityGate.LIQUIDITY,))

    context = assess_risk_context(connection, security_id, as_of=AS_OF)

    assert set(context.invalidation.signals_raised()) == {
        BACKWARD_TRANSITION,
        LOST_ELIGIBILITY,
    }
    assert context.invalidation.current_state is MarketState.CONSOLIDATION


def test_the_result_stamps_the_configuration_that_produced_it(connection, register):
    """A stored assessment must be re-derivable, matching Modules 08-11."""
    security_id = register("STAMP")

    context = assess_risk_context(connection, security_id, as_of=AS_OF)

    assert context.config_version == RiskConfig().version_label()
    assert context.calibration_status == "UNVALIDATED_PLACEHOLDERS"
    assert context.as_dict()["config_version"] == RiskConfig().version_label()


def test_module_11_similarity_is_deliberately_not_consulted(connection, register):
    """Recorded in the result rather than only in a docstring.

    Until Module 17 populates the case dataset there is nothing to read,
    and a pass-through channel that is empty by construction is worse
    than an explicit absence — it would look like evidence of no risk.
    """
    security_id = register("NOSIM")

    metadata = assess_risk_context(connection, security_id, as_of=AS_OF).metadata

    assert metadata["similarity_evidence_consulted"] is False


def test_the_assessment_is_point_in_time_end_to_end(connection, register, add_event):
    """One call, two dates, two different honest answers."""
    security_id = register("PITEND")
    add_event(
        security_id,
        scheduled_for=AS_OF + timedelta(days=5),
        available=AS_OF - timedelta(days=1),
    )

    before = assess_risk_context(connection, security_id, as_of=AS_OF - timedelta(days=30))
    after = assess_risk_context(connection, security_id, as_of=AS_OF)

    assert before.flag(EVENT_PROXIMITY).raised is None
    assert before.flag(EVENT_PROXIMITY).unavailable is MissReason.NOT_YET_AVAILABLE
    assert after.flag(EVENT_PROXIMITY).raised is True


def test_module_08s_own_evidence_travels_into_the_assessment(connection, register):
    """A flag that could not be determined is best explained by the
    record that says why the feature was uncomputable."""
    security_id = register("EVID")
    vector = _vector(
        security_id,
        {"avg_dollar_volume": 2_000_000.0, "atr_percentile": None},
        unavailable=("atr_percentile", "volatility_compression"),
    )

    context = assess_risk_context(connection, security_id, as_of=AS_OF, features=vector)

    assert context.flag(VOLATILITY_SPIKE).raised is None
    assert context.flag(LIQUIDITY_DEGREE).raised is False
    evidence = context.metadata["feature_evidence"]
    assert evidence["unavailable_features"] == ["atr_percentile", "volatility_compression"]
