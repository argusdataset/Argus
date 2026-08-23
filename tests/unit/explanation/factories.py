"""Explanation inputs built from the real upstream modules. Not a test module.

Every fixture here goes through Module 13's actual scoring engine and
Module 15's actual case assembly rather than hand-writing the dicts. A
hand-written input would test this module against my belief about the
other five, and the whole premise of Module 16 is that those five already
contain the reasoning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from core.explanation.facts import similarity_as_dict
from core.scoring.engine import score_candidate
from tests.unit.scoring.factories import (
    AS_OF,
    HEALTHY_FEATURES,
    adequate,
    insufficient,
    lineage,
    risk_context,
    scoring_inputs,
    similarity,
    sparse,
)

#: Module 13's worked high-score/low-confidence example: everything
#: structural is excellent, the evidence behind it is six cases.
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

STATE_EVIDENCE: dict[str, Any] = {
    "matched_state": "BREAKOUT_READY",
    "predicate_inputs": ["volatility_compression"],
    "target_model": "target-model-v1",
    "target_model_assessment": {
        "assessed": True,
        "quality": 0.62,
        "components": {"prior_decline": 0.53, "consolidation": 0.84},
        "supports_advancement": True,
        "unavailable_inputs": [],
    },
    "calibration_status": "UNVALIDATED_PLACEHOLDERS",
}


def signal_bundle(
    *,
    cross=None,
    security_id: UUID | None = None,
    with_similarity: bool = True,
    with_risk: bool = True,
    with_state: bool = True,
    **kwargs,
) -> dict[str, Any]:
    """A scored (or refused) signal plus the evidence around it."""
    security_id = security_id or uuid4()
    cross = adequate() if cross is None else cross
    signal = score_candidate(
        scoring_inputs(security_id, cross=cross, **kwargs),
        as_of=AS_OF,
        lineage=lineage(),
    )
    return {
        "signal": signal.as_dict(),
        "similarity": (
            similarity_as_dict(similarity(security_id, cross)) if with_similarity else None
        ),
        "risk": risk_context(security_id, **_risk_kwargs(kwargs)).as_dict() if with_risk else None,
        "state_evidence": STATE_EVIDENCE if with_state else None,
    }


def high_score_low_confidence() -> dict[str, Any]:
    """argus_score around 88, confidence around 34 — Module 13's example."""
    return signal_bundle(
        cross=sparse(0.05),
        quality=1.0,
        feature_values=STRONG_FEATURES,
        feature_coverage=0.6,
        liquidity=0.0,
        expansion=1.0,
        days_to_event=2.0,
    )


def refused() -> dict[str, Any]:
    """The state ARGUS is actually in today: nothing scoreable."""
    return signal_bundle(cross=insufficient())


def failed_case() -> dict[str, Any]:
    """A completed FAILED case with a false-positive classification."""
    from core.outcome_tracking.case_record import CaseRecord, StageChecklist
    from core.outcome_tracking.classification import COINCIDENT, OutcomeClassification
    from core.outcome_tracking.excursion import Excursion, OutcomeWindow
    from infra.db.enums import (
        FalsePositiveType,
        OutcomeStatus,
        ReviewConfidence,
        SetupLifecycleStatus,
    )

    entry = datetime(2024, 1, 30, 21, 0, tzinfo=UTC)
    end = datetime(2024, 3, 26, 21, 0, tzinfo=UTC)
    window = OutcomeWindow(
        entry_at=entry,
        ends_at=end,
        ends_because="terminal_event",
        horizon_ends_at=end + timedelta(days=30),
        terminal_at=end,
    )
    record = CaseRecord(
        setup_id=uuid4(),
        security_id=uuid4(),
        stages=StageChecklist(
            detected_at=datetime(2024, 1, 16, 21, 0, tzinfo=UTC),
            qualified_at=datetime(2024, 1, 17, 21, 0, tzinfo=UTC),
            activated_at=entry,
            concluded_at=end,
            terminal_event_type="endpoint_reached",
            stages_reached=tuple(status.value for status in SetupLifecycleStatus),
            retreat_count=2,
            retreats=(datetime(2024, 2, 14, 21, 0, tzinfo=UTC),),
            events_recorded=6,
        ),
        excursion=Excursion(
            window=window,
            entry_price=100.0,
            exit_price=93.0,
            mfe=0.08,
            mae=-0.09,
            time_to_mfe=timedelta(days=12),
            time_to_mae=timedelta(days=30),
            realized_return=-0.07,
            benchmark_relative_return=-0.11,
            volatility_adjusted_outcome=-0.42,
            stop_hit_at=datetime(2024, 2, 20, 21, 0, tzinfo=UTC),
            bars_observed=40,
        ),
        classification=OutcomeClassification(
            status=OutcomeStatus.FAILED,
            reason="The stop was reached first, at 2024-02-20T21:00:00+00:00.",
            false_positive_type=FalsePositiveType.E_CATALYST_DRIVEN,
            false_positive_reason=(
                "A scheduled EARNINGS event on 2024-02-12 sits within 5 days of the peak "
                "excursion: the move may be the event rather than the pattern."
            ),
            false_positive_confidence=COINCIDENT,
            review_confidence=ReviewConfidence.MEDIUM,
            review_reason="A distortion was flagged.",
        ),
        market_regime_at_outcome="DOWN_TREND",
        events_in_window=({"event_type": "EARNINGS"},),
    )
    return {"case": record.as_dict()}


def successful_case() -> dict[str, Any]:
    """The same shape, resolved the other way."""
    from copy import deepcopy

    from infra.db.enums import OutcomeStatus

    payload = deepcopy(failed_case()["case"])
    payload["classification"] = OutcomeStatus.SUCCESS.value
    payload["verdict"]["status"] = OutcomeStatus.SUCCESS.value
    payload["verdict"]["false_positive_type"] = None
    payload["verdict"]["false_positive_reason"] = ""
    payload["verdict"]["false_positive_confidence"] = None
    payload["outcome"]["realized_return"] = 0.14
    payload["outcome"]["mfe"] = 0.19
    return {"case": payload}


def _risk_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """The subset of scoring kwargs that also shape the risk context."""
    allowed = ("liquidity", "expansion", "days_to_event", "event_coverage", "eligibility_trend")
    return {name: kwargs[name] for name in allowed if name in kwargs}
