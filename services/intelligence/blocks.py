"""Turning stored artefacts into response blocks. Assembly only.

Each function takes what `reads.py` loaded and produces one block of the
response. None of them computes anything: a score comes from a `signals`
row, a sufficiency state from a `historical_similarity_results` row, a
risk flag from Module 12's own assessment. Where a value is absent, the
block says which kind of absent it is.

## `INSUFFICIENT_EVIDENCE` is built for, not routed around

Most candidates today produce no score. That is Module 13 working
correctly — it refuses rather than guessing — and `build_score` treats
the refusal as a complete answer with the decision named, not as a
degraded path. The five numbers are absent and `unavailable.reason`
carries the decision, so a client can show different copy for
`INSUFFICIENT_EVIDENCE` than for `GATED_INELIGIBLE`; those are different
situations.

## Never blended

`build_similarity` returns cross-asset and same-asset as separate objects
and offers no combined figure. Module 11 refused to blend them and this is
the last place that refusal could quietly be undone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.market_state.states import WATCHLISTS
from services.intelligence.schemas import (
    ExplanationBlock,
    IntelligenceProvenance,
    RiskBlock,
    ScoreBlock,
    SimilarityBlock,
    SimilarityScope,
    StateBlock,
    Unavailable,
)
from services.shared.schemas import Freshness

__all__ = [
    "NO_SIGNAL",
    "build_explanation",
    "build_freshness",
    "build_risk",
    "build_score",
    "build_similarity",
    "build_state",
    "watchlists_for_state",
]

#: A security ARGUS has never scored. Distinct from one it scored and
#: refused: nothing has looked at it yet.
NO_SIGNAL = "no_signal"

_DECISION_EXPLANATIONS: dict[str, str] = {
    "INSUFFICIENT_EVIDENCE": (
        "ARGUS looked at this candidate and declined to score it: too much of the "
        "scoring weight rests on evidence it could not measure. This is the expected "
        "answer for most securities today, because the historical case dataset the "
        "evidence component reads has not been built yet."
    ),
    "GATED_INELIGIBLE": (
        "This candidate was excluded before scoring: it failed one of the eligibility "
        "gates that decide whether a security is tradeable and trackable at all."
    ),
    "GATED_LOST_ELIGIBILITY": (
        "ARGUS was tracking this and stopped: the security lost eligibility after the "
        "setup was opened, so the structure is no longer one ARGUS will act on."
    ),
}
_NO_SIGNAL_EXPLANATION = (
    "ARGUS has not scored this security. It has not appeared in a candidate pool — "
    "either it is outside the universe, or no scan has reached it yet."
)


def build_freshness(
    *, computed_at: datetime | None, as_of: datetime, stale_after_seconds: float
) -> Freshness:
    """When this answer was true, and whether that is recent enough.

    `computed_at` None means nothing has been computed for this security,
    in which case the answer is as fresh as it will ever be — the age is
    zero and the staleness reason says there is nothing to age.
    """
    if computed_at is None:
        return Freshness(
            computed_at=as_of,
            as_of=as_of,
            age_seconds=0.0,
            stale=False,
            staleness_reason=None,
        )
    age = (as_of - computed_at).total_seconds()
    stale = age > stale_after_seconds
    return Freshness(
        computed_at=computed_at,
        as_of=as_of,
        age_seconds=age,
        stale=stale,
        staleness_reason=(
            f"Last computed {int(age // 86400)} day(s) ago. ARGUS scans once per trading "
            "day; a gap longer than that means this security was not in a recent "
            "candidate pool."
            if stale
            else None
        ),
    )


def build_score(signal: dict[str, Any] | None) -> ScoreBlock:
    """The five numbers, or a complete account of why there are none."""
    if signal is None:
        return ScoreBlock(
            scored=False,
            unavailable=Unavailable(reason=NO_SIGNAL, explanation=_NO_SIGNAL_EXPLANATION),
        )

    decision = signal.get("decision")
    provenance = IntelligenceProvenance(
        note=(
            "The configuration versions this score was produced under. Module 03 makes "
            "all six mandatory so any score can be re-derived exactly."
        ),
        signal_id=signal.get("signal_id"),
        target_model_version_id=signal.get("target_model_version_id"),
        scoring_configuration_id=signal.get("scoring_configuration_id"),
        feature_schema_version_id=signal.get("feature_schema_version_id"),
        data_snapshot_id=signal.get("data_snapshot_id"),
        universe_version_id=signal.get("universe_version_id"),
        detection_configuration_id=signal.get("detection_configuration_id"),
    )

    if decision != "SCORED":
        return ScoreBlock(
            scored=False,
            evidence_status=signal.get("evidence_status"),
            probability_status=signal.get("probability_status"),
            components=signal.get("components") or {},
            weight_coverage=signal.get("weight_coverage"),
            unavailable=Unavailable(
                reason=str(decision or NO_SIGNAL),
                explanation=_DECISION_EXPLANATIONS.get(
                    str(decision),
                    "ARGUS declined to score this candidate. See the components for "
                    "which inputs were unavailable.",
                ),
            ),
            provenance=provenance,
            computed_at=signal.get("event_time"),
        )

    return ScoreBlock(
        scored=True,
        evidence_status=signal.get("evidence_status"),
        argus_score=signal.get("argus_score"),
        confidence=signal.get("confidence"),
        opportunity_score=signal.get("opportunity_score"),
        risk_score=signal.get("risk_score"),
        # Always None. Module 13 stores no probability and this module
        # invents nothing — `probability_status` says why.
        probability=signal.get("probability"),
        probability_status=signal.get("probability_status"),
        components=signal.get("components") or {},
        weight_coverage=signal.get("weight_coverage"),
        provenance=provenance,
        computed_at=signal.get("event_time"),
    )


def build_similarity(scoped: dict[str, dict[str, Any]]) -> SimilarityBlock:
    """Both scopes, separately. No combined number exists or is offered."""
    if not scoped:
        return SimilarityBlock(
            unavailable=Unavailable(
                reason="no_similarity_evidence",
                explanation=(
                    "ARGUS has computed no historical analogues for this security. The "
                    "case dataset a comparison reads is built by the full historical "
                    "scan, which has not run."
                ),
            )
        )

    computed = [entry.get("computed_at") for entry in scoped.values() if entry.get("computed_at")]
    return SimilarityBlock(
        cross_asset=_scope(scoped.get("CROSS_ASSET")),
        same_asset=_scope(scoped.get("SAME_ASSET")),
        computed_at=max(computed) if computed else None,
    )


def build_risk(*, flags: dict[str, Any], pending: list[dict[str, Any]]) -> RiskBlock:
    """Itemized risk, with the undetermined flags named rather than dropped.

    Module 12's rule: a flag ARGUS could not evaluate is undetermined, not
    "no risk". The two are indistinguishable to a careless consumer and
    mean opposite things, so the undetermined ones are listed explicitly
    as well as appearing in `flags`.
    """
    undetermined = [
        name
        for name, entry in flags.items()
        if isinstance(entry, dict) and entry.get("value") is None
    ]
    return RiskBlock(
        flags=flags,
        undetermined=undetermined,
        pending_events=pending,
        unavailable=(
            Unavailable(
                reason="no_risk_assessment",
                explanation=(
                    "ARGUS has no stored risk assessment for this security. Risk is "
                    "assessed per candidate during a scan; a security that has not "
                    "been a candidate has none."
                ),
            )
            if not flags and not pending
            else None
        ),
    )


def build_explanation(explanation: Any | None) -> ExplanationBlock:
    """Module 16's output, passed through untouched.

    `as_dict()` and nothing else. Module 16 built a fabrication guard
    around this text — every sentence cites a fact from the input — and
    rewriting, summarising or re-flowing it here would step outside that
    guard while looking harmless.
    """
    if explanation is None:
        return ExplanationBlock(
            kind="",
            subject="",
            unavailable=Unavailable(
                reason="no_explanation",
                explanation=(
                    "There is nothing to explain: ARGUS holds no signal for this "
                    "security, so there is no decision to account for."
                ),
            ),
        )

    payload = explanation.as_dict()
    return ExplanationBlock(
        kind=payload.get("kind", ""),
        subject=payload.get("subject", ""),
        headline=payload.get("headline") or {},
        text=payload.get("text", ""),
        sections=payload.get("sections", []),
        omissions=payload.get("omissions", []),
        facts=payload.get("facts") or {},
    )


def build_state(row: Any | None) -> StateBlock | None:
    """Module 10's projection, and which lists that state puts it on."""
    if row is None:
        return None
    return StateBlock(
        state=str(row.state),
        entered_at=row.entered_at,
        confidence=float(row.confidence) if row.confidence is not None else None,
        evidence={},
        watchlists=watchlists_for_state(str(row.state)),
    )


def watchlists_for_state(state: str) -> list[str]:
    """Which of the four lists a state appears on. Module 10 owns the map.

    Read from `WATCHLISTS` rather than restated, so a state added to a
    list in Module 10 appears here with no change — and a state on no list
    (DISTRIBUTION, UNCLASSIFIED) correctly returns nothing.
    """
    return sorted(
        name
        for name, states in WATCHLISTS.items()
        if any(member.value == state for member in states)
    )


def _scope(entry: dict[str, Any] | None) -> SimilarityScope | None:
    if entry is None:
        return None

    statistics = entry.get("similarity_distribution") or {}
    sufficiency = str(statistics.get("sufficiency") or "INSUFFICIENT")
    reportable = sufficiency != "INSUFFICIENT"

    return SimilarityScope(
        scope=entry["scope"],
        match_count=entry["match_count"],
        sufficiency=sufficiency,
        median_outcome=entry.get("median_outcome") if reportable else None,
        average_outcome=entry.get("average_outcome") if reportable else None,
        failure_rate=entry.get("failure_rate") if reportable else None,
        failure_rate_interval=(statistics.get("failure_rate_interval") if reportable else None),
        outcome_by_regime=entry.get("outcome_by_regime") if reportable else None,
        unavailable=(
            None
            if reportable
            else Unavailable(
                reason=sufficiency,
                explanation=(
                    f"{entry['match_count']} historical analogue(s) — too few for ARGUS to "
                    "report an outcome distribution. The count is shown; the statistics "
                    "are not, because a rate from a handful of cases looks exactly like "
                    "one from thousands."
                ),
                observed=entry["match_count"],
            )
        ),
    )
