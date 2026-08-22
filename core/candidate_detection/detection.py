"""Candidate detection — the coarse, high-recall pre-filter.

Answers exactly one question: *could this contain a setup of interest?*
Not "is this a good setup" (Module 13 scores), not "what state is it in"
(Module 10 classifies), and not "do we have enough evidence to judge it"
(the eligibility gates, which run after this and are deliberately a
separate stage).

## Why high recall, and what that costs

The asymmetry is stark. A dull security that reaches the next stage costs
some compute and is then rejected. A real base that never enters the pool
is invisible to every subsequent module, forever, with no record that it
existed — and ARGUS's entire premise is finding candidates *during* the
base, before the rally, which is precisely when the evidence is weakest
and easiest to filter away. So this stage is tuned to over-include.

Concretely, that shapes two decisions that would otherwise look sloppy:

- **Missing data does not exclude.** A security whose features are half
  absent still enters the pool if it ranks. Adjudicating missing data is
  the `DATA_QUALITY` gate's job, and doing it here as well would blur the
  two questions Module 09 exists to keep apart — you could no longer tell
  a pattern miss from an evidence miss when analysing false negatives.
- **Only one directional requirement.** Everything else is ranking.

## The one directional requirement

`drawdown_pct < 0` — the security is below its trailing structural peak.

This is a *sign* condition, not a magnitude threshold. Zero is not a
tunable constant: it is the definitional boundary between "at its high"
and "off its high", and the ARGUS setup begins with a prior decline, so a
security making new highs cannot be in the phase this module looks for.
`drawdown_pct < -0.30` would be the forbidden shape — an arbitrary depth
that a three-week base and a three-year base would meet differently.

## The ranking, and why it is cross-sectional

Everything else is a within-batch percentile rank, averaged. Percentiles
because the alternative is absolute cutoffs on quantities whose natural
scale moves with the market: a volatility-compression reading that means
"unusually quiet" in 2017 means "wildly volatile" in March 2020. A
cross-sectional rank is self-calibrating — it asks "quiet *compared to
what else is available right now*", which is the question a scan across
one universe on one date should be asking.

It also means the pool is a predictable fraction of the universe, which
is what makes this a usable compute budget rather than a filter that
returns eight securities in a bull market and four thousand in a crash.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pandas as pd

from core.candidate_detection.config import DetectionConfig
from core.candidate_detection.pool import (
    Candidate,
    CandidatePool,
    ExclusionReason,
)
from core.feature_engine.vector import BatchFeatureResult

#: Features the composite ranks on, and the direction that is more
#: setup-like. `ascending=True` means a *smaller* value ranks higher.
#:
#: Each is a Module 08 feature, chosen because it describes the base
#: itself rather than what happened after it:
#:   drawdown_pct           — deeper decline from the structural peak
#:   volatility_compression — recent volatility below the preceding stretch
#:   normalized_range_width — tighter range, scale-free
#:   volume_contraction     — thinner volume than the preceding stretch
#:   atr_percentile         — quiet relative to the security's OWN history
RANKING_COMPONENTS: dict[str, bool] = {
    "drawdown_pct": True,
    "volatility_compression": True,
    "normalized_range_width": True,
    "volume_contraction": True,
    "atr_percentile": True,
}

#: The sole directional requirement. See the module docstring.
REQUIRED_DIRECTION = "drawdown_pct"


def detect_candidates(
    features: BatchFeatureResult,
    *,
    config: DetectionConfig | None = None,
    detection_configuration_id: UUID | None = None,
    run_id: UUID | None = None,
) -> CandidatePool:
    """Select a candidate pool from a batch of Module 08 feature vectors.

    Vectorized throughout: the feature vectors become one
    (securities x components) frame and every rank is a single pandas
    call over the whole universe. There is no per-security branch here
    beyond assembling the result objects, which is the same arrangement
    `core/feature_engine/engine.py` uses and for the same reason.

    Takes an already-computed `BatchFeatureResult` rather than a
    connection: detection performs no I/O at all, which keeps it trivially
    testable and makes the "is this vectorized" question answerable by
    reading it. `as_of` rides along on the feature result, so the live and
    historical-replay paths remain the same call.
    """
    config = config or DetectionConfig()
    run_id = run_id or uuid4()

    excluded: dict[UUID, ExclusionReason] = dict.fromkeys(
        features.missing_securities, ExclusionReason.NO_FEATURES
    )

    frame = _feature_frame(features)
    if frame.empty:
        return CandidatePool(
            as_of=features.as_of,
            run_id=run_id,
            detection_configuration_id=detection_configuration_id,
            candidates={},
            excluded=excluded,
        )

    available = frame.notna().sum(axis=1)
    too_sparse = available < config.detection.min_ranking_components
    for security_id in frame.index[too_sparse]:
        excluded[security_id] = ExclusionReason.INSUFFICIENT_COMPONENTS

    # The one directional requirement. NaN is not a failure of it — a
    # security whose drawdown could not be computed is not thereby known
    # to be at its highs, and excluding it here would turn a data gap
    # into a pattern verdict.
    off_peak = frame[REQUIRED_DIRECTION] < 0
    at_highs = (~off_peak) & frame[REQUIRED_DIRECTION].notna()
    for security_id in frame.index[at_highs & ~too_sparse]:
        excluded[security_id] = ExclusionReason.NOT_OFF_ITS_PEAK

    eligible_to_rank = frame.loc[~too_sparse & ~at_highs]
    if eligible_to_rank.empty:
        return CandidatePool(
            as_of=features.as_of,
            run_id=run_id,
            detection_configuration_id=detection_configuration_id,
            candidates={},
            excluded=excluded,
        )

    ranks = _component_ranks(eligible_to_rank)
    composite = ranks.mean(axis=1, skipna=True)

    selected = _take_top_fraction(composite, config.detection.selection_fraction)
    for security_id in composite.index[~composite.index.isin(selected.index)]:
        excluded[security_id] = ExclusionReason.BELOW_SELECTION_CUT

    candidates = {
        security_id: Candidate(
            security_id=security_id,
            composite_rank=float(value),
            components={
                name: float(rank) for name, rank in ranks.loc[security_id].items() if pd.notna(rank)
            },
            components_available=int(available.get(security_id, 0)),
        )
        for security_id, value in selected.items()
    }

    return CandidatePool(
        as_of=features.as_of,
        run_id=run_id,
        detection_configuration_id=detection_configuration_id,
        candidates=candidates,
        excluded=excluded,
    )


# --------------------------------------------------------------------------
# Internals — all vectorized
# --------------------------------------------------------------------------


def _feature_frame(features: BatchFeatureResult) -> pd.DataFrame:
    """(securities x ranking components), with None becoming NaN.

    One frame for the whole universe is what makes every step below a
    single pandas call rather than a loop.
    """
    if not features.vectors:
        return pd.DataFrame(columns=list(RANKING_COMPONENTS))

    rows = {
        security_id: {name: vector.features.get(name) for name in RANKING_COMPONENTS}
        for security_id, vector in features.vectors.items()
    }
    return pd.DataFrame.from_dict(rows, orient="index", columns=list(RANKING_COMPONENTS)).astype(
        float
    )


def _component_ranks(frame: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional percentile rank per component, 0..1, higher = more setup-like.

    `pct=True` ranks within the batch and ignores NaN, so a security
    missing one component is ranked on the others rather than being
    penalised for the gap — the composite is a mean over what is
    actually present.
    """
    ranks = {}
    for name, smaller_is_better in RANKING_COMPONENTS.items():
        column = frame[name]
        ranked = column.rank(pct=True, ascending=not smaller_is_better)
        ranks[name] = ranked
    return pd.DataFrame(ranks, index=frame.index)


def _take_top_fraction(composite: pd.Series, fraction: float) -> pd.Series:
    """The top `fraction` of the batch by composite rank.

    At least one security is kept whenever anything was ranked: a
    fraction that rounds to zero on a small batch would make the whole
    pipeline silently produce nothing, which is a worse failure than
    keeping one security too many.
    """
    ordered = composite.sort_values(ascending=False)
    keep = max(1, int(round(len(ordered) * fraction)))
    return ordered.iloc[:keep]
