"""Reading Module 10's target-model assessment back out of its result.

Module 10 computes a `TargetModelAssessment` per security, uses its
`quality` as the state's `confidence`, serializes the whole thing into the
transition row's `evidence` JSONB via `assessment_evidence()` — and then
discards the typed object. `ClassificationResult` exposes states and
assignments; it does not expose assessments.

That leaves this module, whose largest component is the pattern match, with
three ways to get the number and only one good one:

1. **Re-run the target model.** Duplicates Module 10's work and would
   silently diverge the day the model changes.
2. **Read `StateAssignment.confidence`.** Numerically the same float, but
   the Module 13 brief is explicit that Module 10's state confidence must
   not be fed into `argus_score` as though it were measured evidence.
   Reading that field would look exactly like the forbidden thing even
   where it is not, and the next person to read the code could not tell
   the difference.
3. **Read the assessment out of the evidence Module 10 published.** Which
   is what this does.

The third names what the number is. It also recovers `components` and
`unavailable_inputs`, which the `confidence` float alone throws away, so
"why is Pattern Quality 62" stays answerable.

The underlying awkwardness — that a typed seam exists between Module 10
and its model but not between Module 10 and its consumers — is flagged in
the module README rather than fixed here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from core.market_state.classifier import ClassificationResult, StateAssignment
from core.market_state.target_model_matching.interface import TargetModelAssessment

#: The key `assessment_evidence()` writes its payload under.
EVIDENCE_KEY = "target_model_assessment"


def assessment_from_state(assignment: StateAssignment) -> TargetModelAssessment | None:
    """Module 10's assessment for one security, or None if it made none.

    None covers both cases Module 10 distinguishes: the security's state
    lies outside the model's covered slice, and the model was asked but
    too few of its inputs were present. Either way there is no pattern
    quality, which is different from a low one — so the Pattern Quality
    component comes back unmeasured rather than zero.
    """
    payload = assignment.evidence.get(EVIDENCE_KEY)
    if not isinstance(payload, dict) or not payload.get("assessed"):
        return None
    return TargetModelAssessment(
        security_id=assignment.security_id,
        quality=_optional_float(payload.get("quality")),
        components={
            name: float(value) for name, value in (payload.get("components") or {}).items()
        },
        supports_advancement=bool(payload.get("supports_advancement", False)),
        unavailable_inputs=tuple(payload.get("unavailable_inputs") or ()),
    )


def assessments_from_classification(
    result: ClassificationResult,
) -> dict[UUID, TargetModelAssessment]:
    """Every assessment in a classification run, by security.

    Securities the model did not assess are absent rather than mapped to
    None, matching how Module 10's own `assess()` returns its dictionary.
    """
    found = {}
    for security_id, assignment in result.assignments.items():
        assessment = assessment_from_state(assignment)
        if assessment is not None:
            found[security_id] = assessment
    return found


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
