"""Combining the evidence into five numbers, or honestly into none.

## The five numbers, and why they are five

```
argus_score        composite setup quality, for ranking
confidence         how reliable the evidence behind it is
opportunity_score  potential upside quality
risk_score         relative risk level
probability        a separately calibrated model's output
```

**`argus_score` is never a probability.** An 87 does not mean an 87%
chance of anything. It is a weighted combination of eight component
judgements, most of them unvalidated, and the only number in ARGUS that
could ever be a probability is `probability` — which is `None` here and
will stay `None` until a model is calibrated against real outcomes after
Module 17. Its *definition* is stored anyway, because the structure being
present and the value being honestly absent is different from the concept
not existing.

`opportunity_score` and `risk_score` are computed from their own upstream
evidence, not carved out of `argus_score` afterwards. `confidence` is
computed in `confidence.py` from inputs no component touches.

## What a score is worth is decided before it is computed

`gating.py` refuses first. A candidate that lost eligibility gets no
numbers at all; a candidate whose risk inputs were undetermined gets no
numbers at all. This ordering is the point: a number that should not
exist is never produced and then suppressed, it is never produced.

## Reproducibility

Every result carries the full six-part lineage, and re-running the same
inputs under the same configuration produces an identical result — the
computation is a pure function of `ScoringInputs` and `ScoringConfig`.
The IDs are what make that checkable years later, when the placeholder
weights have been replaced twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from core.candidate_detection.eligibility.gates import EligibilityOutcome
from core.scoring.components import (
    COMPONENT_NAMES,
    Component,
    ScoringInputs,
    compute_components,
    risk_level,
)
from core.scoring.confidence import ConfidenceAssessment, compute_confidence
from core.scoring.config import (
    PROBABILITY_DEFINITION,
    PROBABILITY_STATUS,
    ScoringConfig,
    stored_probability_definition,
)
from core.scoring.gating import (
    GateVerdict,
    ScoringDecision,
    post_scoring_gates,
    pre_scoring_gates,
    weight_coverage,
)
from infra.db.enums import EvidenceStatus

CALIBRATION_STATUS = "UNVALIDATED_PLACEHOLDERS"


@dataclass(frozen=True, slots=True)
class Lineage:
    """The six IDs that make a stored signal reproducible.

    All six are required. Module 03 made every one of them `NOT NULL` on
    `signals`, and the reason is the whole point of the table: a score
    whose configuration or data cutoff cannot be recovered is a number
    nobody can ever check.
    """

    target_model_version_id: UUID
    feature_schema_version_id: UUID
    data_snapshot_id: UUID
    scoring_configuration_id: UUID
    universe_version_id: UUID
    detection_configuration_id: UUID

    def as_dict(self) -> dict[str, str]:
        return {
            "target_model_version_id": str(self.target_model_version_id),
            "feature_schema_version_id": str(self.feature_schema_version_id),
            "data_snapshot_id": str(self.data_snapshot_id),
            "scoring_configuration_id": str(self.scoring_configuration_id),
            "universe_version_id": str(self.universe_version_id),
            "detection_configuration_id": str(self.detection_configuration_id),
        }


@dataclass(frozen=True, slots=True)
class ScoredSignal:
    """One candidate's scoring result — numbers, or an honest refusal."""

    security_id: UUID
    event_time: datetime
    decision: ScoringDecision
    lineage: Lineage
    #: All None unless `decision is SCORED`.
    argus_score: float | None = None
    confidence: float | None = None
    opportunity_score: float | None = None
    risk_score: float | None = None
    #: Always None. A calibrated model does not exist yet, and a
    #: plausible-looking placeholder here would be the single most
    #: damaging number in the system.
    probability: float | None = None
    probability_definition: str = PROBABILITY_DEFINITION
    probability_status: str = PROBABILITY_STATUS
    components: dict[str, Component] = field(default_factory=dict)
    confidence_assessment: ConfidenceAssessment | None = None
    weight_coverage: float = 0.0
    verdict: GateVerdict | None = None
    calibration_status: str = CALIBRATION_STATUS

    @property
    def evidence_status(self) -> EvidenceStatus | None:
        return self.decision.evidence_status

    @property
    def writes_signal(self) -> bool:
        return self.decision.writes_signal

    def component_value(self, name: str) -> float | None:
        component = self.components.get(name)
        return None if component is None else component.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "security_id": str(self.security_id),
            "event_time": self.event_time.isoformat(),
            "decision": self.decision.value,
            "evidence_status": (self.evidence_status.value if self.evidence_status else None),
            "argus_score": self.argus_score,
            "confidence": self.confidence,
            "opportunity_score": self.opportunity_score,
            "risk_score": self.risk_score,
            "probability": self.probability,
            "probability_definition": self.probability_definition,
            "probability_status": self.probability_status,
            "components": {
                name: self.components[name].as_dict()
                for name in COMPONENT_NAMES
                if name in self.components
            },
            "confidence_assessment": (
                self.confidence_assessment.as_dict() if self.confidence_assessment else None
            ),
            "weight_coverage": self.weight_coverage,
            "verdict": self.verdict.as_dict() if self.verdict else None,
            "lineage": self.lineage.as_dict(),
            "calibration_status": self.calibration_status,
            "note": (
                "argus_score is a composite ranking score, never a probability. "
                "probability is null and stays null until a model is calibrated "
                "against real outcomes after Module 17."
            ),
        }


def score_candidate(
    inputs: ScoringInputs,
    *,
    as_of: datetime,
    lineage: Lineage,
    config: ScoringConfig | None = None,
    eligibility: EligibilityOutcome | None = None,
) -> ScoredSignal:
    """Score one candidate, or refuse to.

    `as_of` is a plain argument, per `CROSS_CUTTING_REQUIREMENTS.md`. All
    the point-in-time work happened upstream — every input here was
    already bounded by `as_of` when it was gathered — and this function
    performs no I/O at all, which is what makes it a pure function of its
    inputs and therefore reproducible.
    """
    config = config or ScoringConfig()
    security_id = inputs.risk.security_id

    refusal = pre_scoring_gates(inputs.risk, eligibility=eligibility)
    if refusal is not None:
        return ScoredSignal(
            security_id=security_id,
            event_time=as_of,
            decision=refusal.decision,
            lineage=lineage,
            verdict=refusal,
        )

    components = compute_components(inputs, config)
    coverage = weight_coverage(components)

    risk = risk_level(inputs, config.normalizers)
    opportunity = _opportunity(inputs, config)
    assessment = compute_confidence(inputs, weight_coverage=coverage, config=config)

    refusal = post_scoring_gates(
        components,
        coverage=coverage,
        risk_score=risk.value,
        opportunity_score=opportunity,
        confidence=assessment.value,
        config=config,
    )
    if refusal is not None:
        return ScoredSignal(
            security_id=security_id,
            event_time=as_of,
            decision=refusal.decision,
            lineage=lineage,
            components=components,
            confidence_assessment=assessment,
            weight_coverage=coverage,
            verdict=refusal,
        )

    return ScoredSignal(
        security_id=security_id,
        event_time=as_of,
        decision=ScoringDecision.SCORED,
        lineage=lineage,
        argus_score=_composite(components),
        confidence=assessment.value,
        opportunity_score=opportunity,
        risk_score=risk.value,
        components=components,
        confidence_assessment=assessment,
        weight_coverage=coverage,
    )


def score_candidates(
    candidates: list[ScoringInputs],
    *,
    as_of: datetime,
    lineage: Lineage,
    config: ScoringConfig | None = None,
    eligibility: dict[UUID, EligibilityOutcome] | None = None,
) -> list[ScoredSignal]:
    """Score a candidate list. No shared state between candidates.

    Deliberately a loop rather than a vectorized pass: the components are
    already computed upstream in batch, and what happens here is per
    candidate branching on availability. A vectorized version would have
    to represent "this one refused and that one did not" as a mask, which
    is how the refusals would eventually get lost.
    """
    config = config or ScoringConfig()
    eligibility = eligibility or {}
    return [
        score_candidate(
            candidate,
            as_of=as_of,
            lineage=lineage,
            config=config,
            eligibility=eligibility.get(candidate.risk.security_id),
        )
        for candidate in candidates
    ]


def decision_counts(signals: list[ScoredSignal]) -> dict[str, int]:
    """How a scoring run came out, by decision. For reporting a scan."""
    counts = dict.fromkeys((decision.value for decision in ScoringDecision), 0)
    for signal in signals:
        counts[signal.decision.value] += 1
    return counts


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _composite(components: dict[str, Component]) -> float:
    """The weighted sum, renormalized over the components that measured.

    Renormalizing rather than treating an absent component as zero: a
    missing component is not a bad one. The renormalization is only ever
    small, because `min_weight_coverage` has already refused anything
    where much of the weight went missing — that threshold and this
    arithmetic are two halves of one decision.
    """
    measured = [
        component
        for component in components.values()
        if component.contributes and component.measured
    ]
    total = sum(component.weight for component in measured)
    if total <= 0.0:  # pragma: no cover - unreachable past the coverage gate
        raise ValueError("No measured component carries weight; gating should have refused.")
    return sum(component.value * component.weight for component in measured) / total


def _opportunity(inputs: ScoringInputs, config: ScoringConfig) -> float | None:
    """Upside quality, from Module 11's MFE distribution across analogues.

    Cross-asset only, for the same reason the Historical Evidence
    component is: "how far did comparable setups run" is a cross-sectional
    question, and Module 11 exposes no combined statistic to answer it any
    other way.

    None when the analogue sample was insufficient — which, until Module
    17 runs, is nearly always, and is why nearly every candidate is
    routed to INSUFFICIENT_EVIDENCE rather than scored on the components
    that happen to be available.
    """
    if inputs.similarity is None:
        return None
    mfe = inputs.similarity.cross_asset.statistics.mfe
    if mfe is None:
        return None
    return config.normalizers.opportunity_mfe.apply(mfe.mean)


def stored_definition() -> str:
    """What `signals.probability_definition` receives. Re-exported here so
    the persistence layer and the result object cannot drift."""
    return stored_probability_definition()
