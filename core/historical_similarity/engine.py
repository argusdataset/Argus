"""The similarity search, and the separation it must never break.

## Cross-asset and same-asset are different kinds of evidence

Eighty-four other companies that formed this shape and what happened to
them is one thing. This company having formed this shape three times
before is a different thing — and specifically, it is **not** automatically
reinforcing. A company that failed this pattern twice is not "due" for
success on the third attempt; it may simply be a company whose bases
fail. Averaging its own history into the cross-asset pool would let that
signal disappear into a crowd.

So the two are computed separately, returned as separate objects, and
stored as separate rows (`AnalogueScope.CROSS_ASSET` /
`AnalogueScope.SAME_ASSET`, per Module 03's schema). `SimilarityEvidence`
deliberately exposes **no combined count and no blended statistic** — not
because one could not be computed, but because the moment one exists,
downstream code will use it and the distinction is gone.

## Why the same-asset path uses transition history, not just setups

A security's own repeat performances are exactly what Module 10's
transition log records: how many times it entered CONSOLIDATION,
ACCUMULATION, BREAKOUT_WATCH, BREAKOUT_READY, and how many times it
retreated. That history exists even when the security has no *completed*
setups yet, so `SameAssetEvidence` carries both — the setup-based
analogues where they exist, and the transition facts always.

Module 10's warning is honoured here: this module uses transition
**facts** (which state, how long, how many cycles) and never Module 10's
`confidence`, which is a pattern-match score from unvalidated weights and
is not a similarity signal.

## Explainability

Every match carries its distance and the per-feature contributions that
produced it. "Why is this similar to these 84 cases" is answerable by
naming features, which is the binding MVP constraint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import pandas as pd
from sqlalchemy.engine import Connection

from core.historical_similarity.cases import CaseSet, load_cases, numeric_features
from core.historical_similarity.config import SimilarityConfig
from core.historical_similarity.distance import (
    FeatureContribution,
    FeatureScales,
    estimate_scales,
    explain,
    pairwise_distances,
)
from core.historical_similarity.features import exclusion_report
from core.historical_similarity.statistics import (
    OutcomeStatistics,
    SampleSufficiency,
    summarize,
)
from core.market_state.transitions import backward_transition_count, cycle_count
from infra.db.enums import AnalogueScope, MarketState

#: The states whose repeat entries constitute a security's own history of
#: this pattern, per the Module 11 brief.
SAME_ASSET_STATES: tuple[MarketState, ...] = (
    MarketState.CONSOLIDATION,
    MarketState.ACCUMULATION,
    MarketState.BREAKOUT_WATCH,
    MarketState.BREAKOUT_READY,
)

#: How many nearest matches to keep explanations for. Explanations are
#: for a human reading a result, and nobody reads 84 of them.
EXPLAINED_MATCHES = 10


@dataclass(frozen=True, slots=True)
class SimilarMatch:
    """One historical case judged similar, and why."""

    setup_id: UUID
    security_id: UUID
    detected_at: datetime
    distance: float
    shared_features: int
    #: Features that made this pair close, closest first. Empty for
    #: matches beyond `EXPLAINED_MATCHES`.
    contributions: tuple[FeatureContribution, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "setup_id": str(self.setup_id),
            "security_id": str(self.security_id),
            "detected_at": self.detected_at.isoformat(),
            "distance": self.distance,
            "shared_features": self.shared_features,
            "closest_features": [
                {
                    "feature": c.feature,
                    "candidate_value": c.candidate_value,
                    "case_value": c.case_value,
                    "share": c.share,
                }
                for c in self.contributions
            ],
        }


@dataclass(frozen=True, slots=True)
class ScopedEvidence:
    """Analogues and outcomes for one scope. Never merged with the other."""

    scope: AnalogueScope
    matches: tuple[SimilarMatch, ...]
    statistics: OutcomeStatistics
    #: Cases considered but rejected as too different, and cases that
    #: could not be compared for lack of shared features. Reported
    #: separately: "no analogues because nothing was similar" and "no
    #: analogues because nothing was comparable" are different facts.
    considered: int = 0
    incomparable: int = 0

    @property
    def count(self) -> int:
        return len(self.matches)

    def distance_distribution(self) -> dict[str, Any]:
        """Shape of the match distances — how tight the neighbourhood is."""
        if not self.matches:
            return {"count": 0, "percentiles": {}}
        distances = pd.Series([m.distance for m in self.matches])
        return {
            "count": len(distances),
            "percentiles": {
                str(p): float(distances.quantile(p / 100)) for p in (10, 25, 50, 75, 90)
            },
            "min": float(distances.min()),
            "max": float(distances.max()),
        }


@dataclass(frozen=True, slots=True)
class SameAssetHistory:
    """A security's own pattern history, from Module 10's transition log.

    Facts only — counts and states. Module 10's `confidence` is
    deliberately absent: it is a pattern-match score from unvalidated
    weights, and treating it as a similarity signal would be exactly the
    misuse Module 10's report warned against.
    """

    cycle_counts: dict[str, int] = field(default_factory=dict)
    backward_transitions: int = 0

    @property
    def total_pattern_entries(self) -> int:
        return sum(self.cycle_counts.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_counts": dict(self.cycle_counts),
            "backward_transitions": self.backward_transitions,
            "total_pattern_entries": self.total_pattern_entries,
            "note": (
                "Transition facts only. Module 10 confidence is deliberately "
                "excluded — it is an unvalidated pattern-match score, not a "
                "similarity signal."
            ),
        }


@dataclass(frozen=True, slots=True)
class SimilarityEvidence:
    """The module's answer for one candidate.

    Note what is absent: no combined count, no blended statistic, no
    overall similarity number. Cross-asset and same-asset are reachable
    only as separate objects, by design.
    """

    security_id: UUID
    as_of: datetime
    feature_schema_version_id: UUID
    data_snapshot_id: UUID | None
    cross_asset: ScopedEvidence
    same_asset: ScopedEvidence
    same_asset_history: SameAssetHistory
    #: Which features the metric compared and which it excluded, so a
    #: stored result explains its own comparison space.
    metric_metadata: dict[str, Any] = field(default_factory=dict)

    def scoped(self, scope: AnalogueScope) -> ScopedEvidence:
        return self.cross_asset if scope is AnalogueScope.CROSS_ASSET else self.same_asset


def find_similar_setups(
    connection: Connection,
    security_id: UUID,
    candidate_features: dict[str, float | None],
    as_of: datetime,
    feature_schema_version_id: UUID,
    *,
    config: SimilarityConfig | None = None,
    data_snapshot_id: UUID | None = None,
    cases: CaseSet | None = None,
    include_same_asset_history: bool = True,
) -> SimilarityEvidence:
    """Find historical analogues for one candidate, cross-asset and same-asset.

    `as_of` is a plain argument — the same call searches today or 2015,
    per `CROSS_CUTTING_REQUIREMENTS.md`. `cases` may be supplied to reuse
    one load across many candidates in a batch; omitted, it is loaded.

    `include_same_asset_history` controls the transition-history read,
    which costs one query per state per security. A caller that only wants
    analogue counts — Module 09's eligibility gate — does not read it, and
    paying for it across a ten-thousand-security scan would be fifty
    thousand wasted queries per date.

    Returns evidence, never a score. Module 13 decides what to do with it.
    """
    config = config or SimilarityConfig()
    thresholds = config.thresholds

    if cases is None:
        cases = load_cases(connection, as_of, feature_schema_version_id)

    # Sanitized with the same helper the case loader uses: a caller may
    # pass a payload read straight out of `feature_vectors`, which carries
    # Module 08's nested `_evidence` block alongside the numbers.
    candidate = pd.Series(numeric_features(candidate_features), dtype=float)

    # Scales come from the whole case population, not from either subset,
    # so a feature's units mean the same thing in both scopes. Estimating
    # them separately would make the two distances incomparable — and
    # someone would eventually compare them.
    scales = estimate_scales(cases.features, thresholds)

    cross = _evidence_for(
        AnalogueScope.CROSS_ASSET, candidate, cases.excluding_security(security_id), scales, config
    )
    same = _evidence_for(
        AnalogueScope.SAME_ASSET, candidate, cases.only_security(security_id), scales, config
    )

    return SimilarityEvidence(
        security_id=security_id,
        as_of=as_of,
        feature_schema_version_id=feature_schema_version_id,
        data_snapshot_id=data_snapshot_id,
        cross_asset=cross,
        same_asset=same,
        same_asset_history=(
            load_same_asset_history(connection, security_id, as_of)
            if include_same_asset_history
            else SameAssetHistory()
        ),
        metric_metadata={
            "metric": "robust_scaled_euclidean",
            "max_distance": thresholds.max_distance.value,
            "scale_sample_size": scales.sample_size,
            "excluded_features": exclusion_report(),
            "calibration_status": "UNVALIDATED_PLACEHOLDERS",
        },
    )


def load_same_asset_history(
    connection: Connection, security_id: UUID, as_of: datetime
) -> SameAssetHistory:
    """This security's own pattern-entry counts, bounded by `as_of`.

    Reads Module 10's transition log directly. Module 10 established that
    rows are written only on genuine state change, so these are cycles
    rather than scans, and that direction is stored at write time — this
    module must not, and does not, reinterpret it.
    """
    return SameAssetHistory(
        cycle_counts={
            state.value: cycle_count(connection, security_id, state, as_of=as_of)
            for state in SAME_ASSET_STATES
        },
        backward_transitions=backward_transition_count(connection, security_id, as_of=as_of),
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _evidence_for(
    scope: AnalogueScope,
    candidate: pd.Series,
    cases: CaseSet,
    scales: FeatureScales,
    config: SimilarityConfig,
) -> ScopedEvidence:
    """Distances, the matches within the radius, and their outcome statistics."""
    thresholds = config.thresholds
    if cases.is_empty or candidate.empty:
        return ScopedEvidence(
            scope=scope,
            matches=(),
            statistics=summarize(pd.DataFrame(), thresholds),
            considered=len(cases),
        )

    distances = pairwise_distances(candidate, cases.features, scales, thresholds)
    incomparable = int(distances["distance"].isna().sum())

    within = distances[distances["distance"] <= thresholds.max_distance.value]
    within = within.sort_values("distance")

    matches: list[SimilarMatch] = []
    for position, (setup_id, row) in enumerate(within.iterrows()):
        outcome = cases.outcomes.loc[setup_id]
        matches.append(
            SimilarMatch(
                setup_id=setup_id,
                security_id=outcome["security_id"],
                detected_at=outcome["detected_at"],
                distance=float(row["distance"]),
                shared_features=int(row["shared_features"]),
                contributions=(
                    explain(candidate, cases.features.loc[setup_id], scales, thresholds)
                    if position < EXPLAINED_MATCHES
                    else ()
                ),
            )
        )

    return ScopedEvidence(
        scope=scope,
        matches=tuple(matches),
        statistics=summarize(cases.outcomes.loc[within.index], thresholds),
        considered=len(cases),
        incomparable=incomparable,
    )


def insufficient(evidence: SimilarityEvidence) -> bool:
    """Whether *both* scopes are too sparse to say anything.

    A convenience for callers, and deliberately not a blended statistic —
    it reports that neither scope reached its floor, which is different
    from combining them into one sample.
    """
    return (
        evidence.cross_asset.statistics.sufficiency is SampleSufficiency.INSUFFICIENT
        and evidence.same_asset.statistics.sufficiency is SampleSufficiency.INSUFFICIENT
    )
