"""The containment seam: what a target model is, from the engine's side.

`target-model-v1` is **not a peer to the state machine**. The state engine
decides *which* state a security is in; a target model judges *how well*
the security matches a specific pattern within the four states that model
covers. Keeping that asymmetry in the type system is the whole reason this
interface exists as a Protocol rather than the model being inlined.

The architecture anticipates a second target model someday, evaluated
against the same state machine. This module does **not** build support for
one — no registry, no dispatch, no plugin loading. It only ensures the
first model's code is separable: the engine imports this protocol and
never the implementation.

## The boundary, and where it is genuinely ambiguous

Module 09's report flagged an unresolved boundary between candidate
detection and state classification. The same class of ambiguity appears
here, one layer down, and this module does not resolve it unilaterally.

The Module 10 brief says two things that pull apart under load:

- the model *"judges transitions through the CONSOLIDATION → ACCUMULATION
  → BREAKOUT_WATCH → BREAKOUT_READY portion"* — which reads as the model
  **gating** those transitions;
- the model provides *"the pattern-quality judgment within those states,
  not the state assignment itself (which is the general state engine's
  job)"* — which reads as the model **not** gating them.

Both cannot hold. A model that gates transitions *is* participating in
state assignment.

**What is implemented:** the second reading. The engine assigns state from
structural features alone; the model scores match quality and that score
becomes the state's `confidence`. The model cannot veto a state.

**Where the first reading would attach:** `TargetModelAssessment` carries
`supports_advancement`, which the engine currently records as evidence and
does not act on. Making the model gate transitions means consulting that
one field in `classifier._resolve_state` — a single named place,
deliberately.

This is flagged in the module report rather than settled here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import pandas as pd

from infra.db.enums import MarketState


@dataclass(frozen=True, slots=True)
class TargetModelAssessment:
    """One security's match against a target model, at one `as_of`.

    `quality` is 0..1 and feeds `market_state.confidence`. It is a
    *pattern-match* score, emphatically not an ARGUS score — Module 13
    owns scoring, and nothing here may be read as a prediction.
    """

    security_id: UUID
    #: 0..1, or None when too few of the model's inputs were present. None
    #: rather than 0.0, for the reason Module 08 established: zero is a
    #: measurement, absence is not.
    quality: float | None
    #: Named contributions, so a match is explainable rather than a number.
    components: dict[str, float] = field(default_factory=dict)
    #: Whether the model believes the structure supports moving further
    #: along the sequence. **Recorded, never acted on** — see this
    #: module's docstring on the unresolved boundary.
    supports_advancement: bool = False
    #: Why the model could not assess, when it could not.
    unavailable_inputs: tuple[str, ...] = ()

    @property
    def assessed(self) -> bool:
        return self.quality is not None


@runtime_checkable
class TargetModel(Protocol):
    """What the state engine requires of any target model.

    Deliberately narrow: a name, the states it covers, and a batch
    assessment call. A model that needed more than this from the engine
    would be a peer to it rather than contained by it.
    """

    name: str

    def covered_states(self) -> frozenset[MarketState]:
        """The slice of the state machine this model judges.

        The engine uses this to decide which securities to assess, so a
        model cannot silently opine on states outside its remit.
        """
        ...

    def required_features(self) -> tuple[str, ...]:
        """Module 08 features this model reads.

        The engine unions these with the state predicates' own inputs when
        it builds the feature frame. Without it the engine would have to
        know what the model reads — which is exactly the coupling
        `target_model_matching/` exists to prevent, and a second model
        would need different features anyway.
        """
        ...

    def assess(
        self,
        features: pd.DataFrame,
        states: pd.Series,
        as_of: datetime,
    ) -> dict[UUID, TargetModelAssessment]:
        """Assess every security whose state this model covers.

        `features` is the wide (securities x features) frame the engine
        already built; `states` maps security to assigned state. Passing
        the frame rather than a connection keeps models free of I/O and
        keeps the batch genuinely vectorized.

        `as_of` is a plain argument, per
        `docs/architecture/CROSS_CUTTING_REQUIREMENTS.md`.
        """
        ...


def assessment_evidence(assessment: TargetModelAssessment | None) -> dict[str, Any]:
    """Serialize an assessment for the transition row's `evidence` JSONB.

    Lives here rather than in the model so the storage shape is the
    engine's concern, not something each model reinvents.
    """
    if assessment is None:
        return {"assessed": False, "reason": "state_not_covered_by_target_model"}
    return {
        "assessed": assessment.assessed,
        "quality": assessment.quality,
        "components": assessment.components,
        "supports_advancement": assessment.supports_advancement,
        "unavailable_inputs": list(assessment.unavailable_inputs),
    }


def assessment_from_evidence(
    security_id: UUID, evidence: dict[str, Any] | None
) -> TargetModelAssessment | None:
    """Rebuild an assessment from what `assessment_evidence` stored.

    The inverse lives here for the same reason the forward direction
    does: the storage shape is the engine's concern, and a caller
    reconstructing it by hand would be a second place for the shape to
    drift. Module 17's replay needs this because `StateAssignment` keeps
    the serialized form rather than the object, and Module 13's
    `pattern_quality` component reads the object — without it, a quarter
    of every replayed score would be silently unavailable.

    Returns None for a state the model does not cover, which is the same
    thing `assess()` returns and means the same thing.
    """
    if not evidence or not evidence.get("assessed"):
        return None
    return TargetModelAssessment(
        security_id=security_id,
        quality=evidence.get("quality"),
        components=dict(evidence.get("components") or {}),
        supports_advancement=bool(evidence.get("supports_advancement", False)),
        unavailable_inputs=tuple(evidence.get("unavailable_inputs") or ()),
    )
