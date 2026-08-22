"""Assigning a market state to every eligible security, in one pass.

## What is solid here and what is not

The brief draws a distinction this module takes seriously. The *structure*
— the state machine, the evidence floor, the resolution order, the
UNCLASSIFIED refusal, the batch interface — is engineering, and is built
and tested with the same discipline as everything before it.

The *thresholds* are not. They are unvalidated placeholders, and they live
entirely in `thresholds.py` so that recalibrating them touches no code in
this file. `tests/unit/market_state/test_threshold_isolation.py` scans
this module's source for numeric literals and fails if one appears, which
is what keeps the separation real rather than aspirational.

## Only eligible securities are classified

Module 09 hands over `report.eligible()`. Anything it flagged
`INSUFFICIENT_EVIDENCE` is `UNCLASSIFIED` here — never classified anyway
on data another module already judged insufficient. Doing otherwise would
make the eligibility gates decorative: they would record a refusal that
the next stage quietly overrode.

The same refusal applies a second time, per-state: a security may be
eligible overall and still lack the specific features a given state's
predicate reads. `minimum_features_for_classification` is that floor, and
a security below it for every state is UNCLASSIFIED with the reason
recorded.

## Vectorized

One wide (securities x features) frame; each state predicate is a single
boolean Series over the whole universe; resolution is a masked assignment.
The only per-security work is assembling result objects. Same arrangement
as `core/feature_engine/engine.py` and `core/candidate_detection`.

## `as_of` is a plain argument

No live/batch distinction anywhere. A live scan passes `datetime.now(UTC)`;
a historical replay passes a date in 2015. Same function, same arguments,
same return type — the guarantee
`docs/architecture/CROSS_CUTTING_REQUIREMENTS.md` asks Modules 08-16 for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import pandas as pd

from core.candidate_detection.eligibility.gates import EligibilityReport
from core.feature_engine.vector import BatchFeatureResult
from core.market_state.states import (
    CLASSIFICATION_FEATURES,
    STATE_DEFINITIONS,
    WATCHLISTS,
)
from core.market_state.target_model_matching.interface import (
    TargetModel,
    TargetModelAssessment,
    assessment_evidence,
)
from core.market_state.target_model_matching.models.target_model_v1.model import TargetModelV1
from core.market_state.thresholds import MarketStateConfig
from infra.db.enums import MarketState


@dataclass(frozen=True, slots=True)
class StateAssignment:
    """One security's state at one `as_of`, with why."""

    security_id: UUID
    state: MarketState
    #: 0..1 from the target model where it covers this state; None
    #: otherwise. Never fabricated — an unassessed state has no
    #: confidence, which is different from low confidence.
    confidence: float | None
    #: Everything needed to re-derive the assignment: which predicate
    #: matched, the feature values it read, and the target model's
    #: assessment. Stored on the transition row.
    evidence: dict[str, object]
    #: Present only for UNCLASSIFIED, naming why.
    unclassified_reason: str | None = None

    @property
    def watchlist(self) -> str | None:
        from core.market_state.states import watchlist_for

        return watchlist_for(self.state)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """States for a whole universe at one `as_of`."""

    as_of: datetime
    target_model_version_id: UUID | None
    assignments: dict[UUID, StateAssignment] = field(default_factory=dict)

    def states(self) -> dict[UUID, MarketState]:
        return {sid: a.state for sid, a in self.assignments.items()}

    def in_state(self, *states: MarketState) -> dict[UUID, StateAssignment]:
        wanted = set(states)
        return {sid: a for sid, a in self.assignments.items() if a.state in wanted}

    def watchlist(self, name: str) -> dict[UUID, StateAssignment]:
        """A derived watchlist view — a filter, never a stored list.

        Raises on an unknown name rather than returning empty: a typo
        that silently produced an empty watchlist would look exactly like
        a day with no candidates.
        """
        if name not in WATCHLISTS:
            raise KeyError(f"Unknown watchlist {name!r}. Known: {sorted(WATCHLISTS)}")
        return self.in_state(*WATCHLISTS[name])

    def distribution(self) -> dict[MarketState, int]:
        counts = dict.fromkeys(MarketState, 0)
        for assignment in self.assignments.values():
            counts[assignment.state] += 1
        return counts

    def __len__(self) -> int:
        return len(self.assignments)


def classify_states(
    features: BatchFeatureResult,
    *,
    eligibility: EligibilityReport | None = None,
    config: MarketStateConfig | None = None,
    target_model: TargetModel | None = None,
    target_model_version_id: UUID | None = None,
) -> ClassificationResult:
    """Classify every security in `features`, honouring Module 09's verdict.

    Takes an already-computed `BatchFeatureResult` rather than a
    connection: like Module 09's detection stage, classification performs
    no I/O, which keeps "is this vectorized" answerable by reading it.

    `eligibility` is optional only so the classifier can be exercised
    directly in tests. In the pipeline it is always supplied, and any
    security Module 09 found ineligible comes back UNCLASSIFIED.
    """
    config = config or MarketStateConfig()
    model = target_model or TargetModelV1()
    thresholds = config.states

    ineligible = _ineligible_reasons(eligibility)
    # The union of what the predicates read and what the model reads. The
    # engine does not know which features the model wants — it asks.
    frame = _feature_frame(
        features, tuple(dict.fromkeys(CLASSIFICATION_FEATURES + model.required_features()))
    )

    assignments: dict[UUID, StateAssignment] = {}
    for security_id, reason in ineligible.items():
        assignments[security_id] = _unclassified(security_id, reason)

    # Securities Module 08 could not produce a vector for at all.
    for security_id, miss in features.missing_securities.items():
        if security_id not in assignments:
            assignments[security_id] = _unclassified(security_id, f"no_feature_vector:{miss.value}")

    classifiable = frame.index.difference(pd.Index(list(assignments)))
    if len(classifiable) == 0:
        return ClassificationResult(
            as_of=features.as_of,
            target_model_version_id=target_model_version_id,
            assignments=assignments,
        )

    candidates = frame.loc[classifiable]
    states, matched_inputs, unmatched_reasons = _resolve_states(candidates, thresholds)
    assessments = model.assess(candidates, states, features.as_of)

    for security_id in candidates.index:
        state = states[security_id]
        if state is MarketState.UNCLASSIFIED:
            assignments[security_id] = _unclassified(security_id, unmatched_reasons[security_id])
            continue
        assignments[security_id] = _assign(
            security_id=security_id,
            state=state,
            row=candidates.loc[security_id],
            inputs=matched_inputs[security_id],
            assessment=assessments.get(security_id),
            model_name=model.name,
        )

    return ClassificationResult(
        as_of=features.as_of,
        target_model_version_id=target_model_version_id,
        assignments=assignments,
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _ineligible_reasons(report: EligibilityReport | None) -> dict[UUID, str]:
    """Module 09's refusals, carried through verbatim.

    The gate names travel into the UNCLASSIFIED reason so a security's
    absence from every watchlist is traceable to the specific gate that
    caused it, rather than to a generic "not classified".
    """
    if report is None:
        return {}
    return {
        security_id: "ineligible:" + ",".join(g.value for g in outcome.failed_gates)
        for security_id, outcome in report.ineligible().items()
    }


def _feature_frame(features: BatchFeatureResult, columns: tuple[str, ...]) -> pd.DataFrame:
    """(securities x requested features), None becoming NaN."""
    if not features.vectors:
        return pd.DataFrame(columns=list(columns))
    rows = {
        security_id: {name: vector.features.get(name) for name in columns}
        for security_id, vector in features.vectors.items()
    }
    return pd.DataFrame.from_dict(rows, orient="index", columns=list(columns)).astype(float)


def _resolve_states(
    frame, thresholds
) -> tuple[pd.Series, dict[UUID, tuple[str, ...]], dict[UUID, str]]:
    """Evaluate every predicate, take the first match in precedence order.

    Vectorized: each predicate produces one boolean Series over the whole
    universe, and assignment is a masked write. A security is only
    eligible for a state whose declared inputs it actually has enough of —
    the per-state evidence floor.

    Also returns, for anything left UNCLASSIFIED, *which kind* of
    UNCLASSIFIED it is. Two very different situations end at the same
    state and must not be conflated:

    - `insufficient_features_for_any_state` — the evidence was too thin to
      judge, the same refusal Module 09 makes.
    - `no_state_predicate_matched` — the evidence was fine and no state
      claimed the security. That is a **gap in the state machine**, not a
      data problem, and it is invisible unless named. Counting these is
      how a modeling hole gets found; folding them in with the thin-data
      cases would hide it permanently.
    """
    resolved = pd.Series(MarketState.UNCLASSIFIED, index=frame.index, dtype=object)
    matched: dict[UUID, tuple[str, ...]] = {}
    undecided = pd.Series(True, index=frame.index)
    had_evidence_somewhere = pd.Series(False, index=frame.index)
    floor = thresholds.minimum_features_for_classification.value

    for definition in STATE_DEFINITIONS:
        present = frame[list(definition.inputs)].notna().sum(axis=1) / len(definition.inputs)
        has_evidence = present >= floor
        had_evidence_somewhere |= has_evidence
        # `fillna(False)` because a predicate over NaN yields NaN, and an
        # unknown must never read as a match.
        hit = definition.predicate(frame, thresholds).fillna(False).astype(bool)
        selected = undecided & has_evidence & hit

        resolved[selected] = definition.state
        for security_id in frame.index[selected]:
            matched[security_id] = definition.inputs
        undecided &= ~selected

    reasons = {
        security_id: (
            "no_state_predicate_matched"
            if had_evidence_somewhere[security_id]
            else "insufficient_features_for_any_state"
        )
        for security_id in frame.index[undecided]
    }
    return resolved, matched, reasons


def _assign(
    *,
    security_id: UUID,
    state: MarketState,
    row: pd.Series,
    inputs: tuple[str, ...],
    assessment: TargetModelAssessment | None,
    model_name: str,
) -> StateAssignment:
    return StateAssignment(
        security_id=security_id,
        state=state,
        confidence=assessment.quality if assessment else None,
        evidence={
            "matched_state": state.value,
            "predicate_inputs": list(inputs),
            "feature_values": {
                name: (None if pd.isna(row[name]) else float(row[name])) for name in inputs
            },
            "target_model": model_name,
            "target_model_assessment": assessment_evidence(assessment),
            # Recorded on every row so a stored state can never be
            # mistaken for one produced by validated thresholds.
            "calibration_status": "UNVALIDATED_PLACEHOLDERS",
        },
    )


def _unclassified(security_id: UUID, reason: str) -> StateAssignment:
    return StateAssignment(
        security_id=security_id,
        state=MarketState.UNCLASSIFIED,
        confidence=None,
        evidence={"unclassified_reason": reason},
        unclassified_reason=reason,
    )
