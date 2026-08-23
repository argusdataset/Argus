"""What must be true before a candidate gets numbers at all.

Three refusals, in order, and they are different kinds of refusal:

1. **Ineligible** — Module 09's gates said no. Reaffirmed here rather than
   re-derived: this module reads the outcome, it does not re-run a gate.
2. **Lost eligibility** — Module 12's invalidation signal. Gated, not
   penalised, for the reason Module 12's report gave: folding "this
   thesis has broken" into a continuous score as one input among several
   would let a strong reading elsewhere numerically outweigh it. A broken
   thesis is a lifecycle fact, and Module 14 acts on it.
3. **Insufficient evidence** — enough of the score could not be measured
   for the composite to mean anything.

## Why the first two produce no signal row

Module 03's `signals` table has two evidence statuses, `SCORED` and
`INSUFFICIENT_EVIDENCE`, and its comment says "no data is the absence of a
signal row entirely". Writing an `INSUFFICIENT_EVIDENCE` row for a
candidate that lost eligibility would say ARGUS could not gather evidence,
which is false — it gathered evidence and the evidence says the setup is
over. So the gated decisions return a verdict to the caller and write
nothing, keeping `INSUFFICIENT_EVIDENCE` meaning only what it says.

This module reports the gate outcome. It does not record a lifecycle
transition; that is Module 14's, and doing it here would be exactly the
boundary the brief draws.

## Why insufficient evidence is decided by weight, not by a checklist

A candidate can be missing any of a dozen readings. What matters is not
how many but how much of the score they were supposed to carry, so the
threshold is on **coverage of the active weight**. `min_weight_coverage`
is set above 0.80 deliberately: the historical-evidence component alone
carries 0.20, so its absence is on its own enough to refuse a composite.
Until Module 17 populates the case dataset that is nearly every candidate
— which is the correct current behaviour, and there is a test asserting
it rather than working around it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core.candidate_detection.eligibility.gates import EligibilityOutcome
from core.risk_context.assessment import RiskContext
from core.scoring.components import Component
from core.scoring.config import ScoringConfig
from infra.db.enums import EvidenceStatus


class ScoringDecision(StrEnum):
    """What happened to a candidate. Finer-grained than `EvidenceStatus`.

    `EvidenceStatus` has two values because a stored signal row has two
    honest states. This enum has four because *not writing a row* has two
    distinct reasons, and collapsing them would lose the difference
    between "we could not judge" and "there is nothing left to judge".
    """

    SCORED = "SCORED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    #: Module 09's gates rejected it. No row.
    GATED_INELIGIBLE = "GATED_INELIGIBLE"
    #: Module 12 says it passed eligibility before and fails now. No row.
    GATED_LOST_ELIGIBILITY = "GATED_LOST_ELIGIBILITY"

    @property
    def writes_signal(self) -> bool:
        return self in (ScoringDecision.SCORED, ScoringDecision.INSUFFICIENT_EVIDENCE)

    @property
    def evidence_status(self) -> EvidenceStatus | None:
        if self is ScoringDecision.SCORED:
            return EvidenceStatus.SCORED
        if self is ScoringDecision.INSUFFICIENT_EVIDENCE:
            return EvidenceStatus.INSUFFICIENT_EVIDENCE
        return None


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """A refusal, with why."""

    decision: ScoringDecision
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


def pre_scoring_gates(
    risk: RiskContext,
    *,
    eligibility: EligibilityOutcome | None = None,
) -> GateVerdict | None:
    """Refusals that apply before anything is computed. None means proceed.

    `eligibility` is optional because Module 09's outcome object is not
    always in hand at scoring time — the persisted history Module 12 reads
    is. When it is supplied it is authoritative for *now*; Module 12's
    trend is authoritative for *change over time*, and both are checked.
    """
    if eligibility is not None and not eligibility.eligible:
        return GateVerdict(
            decision=ScoringDecision.GATED_INELIGIBLE,
            reason="Module 09's eligibility gates rejected this candidate.",
            detail={"failed_gates": [gate.value for gate in eligibility.failed_gates]},
        )

    change = risk.invalidation.eligibility
    if change.lost:
        return GateVerdict(
            decision=ScoringDecision.GATED_LOST_ELIGIBILITY,
            reason=(
                "This candidate passed eligibility previously and no longer does. "
                "A broken thesis is gated, not scored with a penalty: a strong "
                "reading elsewhere must not be able to outweigh it."
            ),
            detail={
                "trend": change.trend.value,
                "last_passing_at": (
                    change.last_passing_at.isoformat() if change.last_passing_at else None
                ),
                "failed_gates": (
                    [gate.value for gate in change.latest.failed_gates] if change.latest else []
                ),
                "newly_failed_gates": [gate.value for gate in change.newly_failed_gates],
                "routed_to": "Module 14 (lifecycle)",
            },
        )

    return None


def weight_coverage(components: dict[str, Component]) -> float:
    """Share of the active component weight that was actually measured.

    Zero-weight components are outside both numerator and denominator:
    `fundamental_context` is inactive by design, and letting its permanent
    absence count as missing coverage would make every candidate look
    partially unmeasured forever.
    """
    active = [component for component in components.values() if component.contributes]
    total = sum(component.weight for component in active)
    if total <= 0.0:
        return 0.0
    measured = sum(component.weight for component in active if component.measured)
    return measured / total


def post_scoring_gates(
    components: dict[str, Component],
    *,
    coverage: float,
    risk_score: float | None,
    opportunity_score: float | None,
    confidence: float | None,
    config: ScoringConfig,
) -> GateVerdict | None:
    """Refusals that only become visible once the components are computed.

    The order is deliberate: the reason a candidate is unscored should be
    the most specific true thing, and "risk inputs were undetermined" is
    more useful than "coverage was 0.78".

    Every one of these maps onto Module 03's CHECK constraint, which
    requires a `SCORED` row to carry all four numbers. Rather than
    discovering that at INSERT time, the refusal happens here where the
    reason can be recorded.
    """
    if risk_score is None:
        missing = components["risk_reward"].unavailable
        return GateVerdict(
            decision=ScoringDecision.INSUFFICIENT_EVIDENCE,
            reason=(
                "Risk inputs were undetermined, so no honest risk_score exists. "
                "Module 12 reports an unmeasured risk flag as unmeasured, never as "
                "safe, and a candidate ARGUS knows nothing about must not be able "
                "to outrank one that was measured and came back clean."
            ),
            detail={"undetermined_risk_inputs": list(missing)},
        )

    threshold = config.thresholds.min_weight_coverage.value
    if coverage < threshold:
        return GateVerdict(
            decision=ScoringDecision.INSUFFICIENT_EVIDENCE,
            reason=(
                f"Only {coverage:.2f} of the active component weight could be "
                f"measured, below the {threshold:.2f} floor."
            ),
            detail={
                "weight_coverage": coverage,
                "min_weight_coverage": threshold,
                "unmeasured_components": [
                    component.name
                    for component in components.values()
                    if component.contributes and not component.measured
                ],
            },
        )

    if opportunity_score is None:
        return GateVerdict(
            decision=ScoringDecision.INSUFFICIENT_EVIDENCE,
            reason=(
                "No opportunity_score: Module 11 reported no MFE distribution for "
                "this candidate's analogues."
            ),
            detail={},
        )

    if confidence is None:
        return GateVerdict(
            decision=ScoringDecision.INSUFFICIENT_EVIDENCE,
            reason="No confidence factor could be measured.",
            detail={},
        )

    return None
