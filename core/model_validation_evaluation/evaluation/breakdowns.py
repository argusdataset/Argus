"""Splitting the population: by regime, and by same-asset vs cross-asset evidence.

## Regime

"Does this work" has no single answer, and reporting one would hide the
answer that matters. A base-and-breakout pattern is a different
proposition in an uptrend than in a distribution phase, and a model that
only works in one is a model with a stated operating range rather than a
broken one — but only if the breakdown exists to say so.

Split on `market_regime_at_outcome`, Module 15's record of the market
state the setup resolved in. Not the regime at detection: the question is
which conditions the *outcome* was realized under.

## Same-asset vs cross-asset

Module 11 keeps these separate and refuses to blend them, on the grounds
that "this security did this before" and "similar securities did this
before" are different kinds of evidence. That distinction survives into
evaluation: a security with prior concluded setups is being evaluated
against its own history, and one without is being evaluated purely on
cross-asset analogy. If ARGUS works only on securities it has already seen
complete a cycle, that is a severe limitation on a 10,000-name universe
and it would be invisible in a pooled number.

## Every bucket carries its own sufficiency

Splitting a population makes every part smaller, which is precisely when
a per-bucket sample floor stops being a formality. A regime with eleven
setups reports eleven and INSUFFICIENT — never a rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from core.historical_similarity.statistics import SampleSufficiency
from core.model_validation_evaluation.evaluation.config import EvaluationConfig
from core.model_validation_evaluation.evaluation.metrics import MetricSuite, compute_metrics

#: Securities with at least this many earlier concluded setups are being
#: evaluated against their own history. One is the whole point: the
#: distinction is "has this security ever completed a cycle before", and
#: the first prior setup is what creates same-asset evidence at all.
SAME_ASSET_MINIMUM = 1

CROSS_ASSET_ONLY = "cross_asset_only"
SAME_ASSET_SUPPORTED = "same_asset_supported"


@dataclass(frozen=True, slots=True)
class Breakdown:
    """One split of the population, keyed by whatever it split on."""

    dimension: str
    groups: dict[str, MetricSuite] = field(default_factory=dict)

    def reportable(self) -> dict[str, MetricSuite]:
        return {key: suite for key, suite in self.groups.items() if suite.reportable}

    def thin(self) -> dict[str, int]:
        """Groups that exist but cannot be reported, and their sizes.

        Returned separately rather than omitted, because "the 90-100
        bucket in DISTRIBUTION holds three setups" is itself a finding
        about where the evidence is not.
        """
        return {
            key: suite.sample_size for key, suite in self.groups.items() if not suite.reportable
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "groups": {key: suite.as_dict() for key, suite in self.groups.items()},
            "reportable": sorted(self.reportable()),
            "thin": self.thin(),
        }


def by_regime(frame: pd.DataFrame, config: EvaluationConfig | None = None) -> Breakdown:
    """Metrics per market regime the setup's outcome was realized in."""
    config = config or EvaluationConfig()
    if frame.empty:
        return Breakdown(dimension="market_regime_at_outcome")

    regimes = frame["market_regime_at_outcome"].fillna("UNRECORDED")
    return Breakdown(
        dimension="market_regime_at_outcome",
        groups={
            str(regime): compute_metrics(frame[regimes == regime], config)
            for regime in sorted(regimes.unique())
        },
    )


def by_evidence_scope(frame: pd.DataFrame, config: EvaluationConfig | None = None) -> Breakdown:
    """Metrics split by whether the security had prior concluded setups.

    Prior setups are counted from the frame itself, by detection order,
    rather than re-queried: the dataset already holds every concluded
    setup for the period, and a security's position within its own
    sequence is a property of that frame. A setup is same-asset-supported
    if this security has at least `SAME_ASSET_MINIMUM` setups that
    concluded before this one was detected.
    """
    config = config or EvaluationConfig()
    if frame.empty:
        return Breakdown(dimension="evidence_scope")

    scope = _evidence_scope(frame)
    return Breakdown(
        dimension="evidence_scope",
        groups={
            key: compute_metrics(frame[scope == key], config)
            for key in (CROSS_ASSET_ONLY, SAME_ASSET_SUPPORTED)
            if (scope == key).any()
        },
    )


def by_regime_and_bucket(
    frame: pd.DataFrame, config: EvaluationConfig | None = None
) -> dict[str, Any]:
    """Score buckets within each regime.

    The cross-tab the calibration work actually needs: whether the score
    orders outcomes *within* a regime, rather than mostly detecting which
    regime the setup was in. Almost every cell will be INSUFFICIENT at any
    realistic scale, and that is the honest answer rather than a defect —
    the structure is here so that a real run can fill it.
    """
    from core.model_validation_evaluation.evaluation.buckets import build_buckets

    config = config or EvaluationConfig()
    if frame.empty:
        return {}

    regimes = frame["market_regime_at_outcome"].fillna("UNRECORDED")
    return {
        str(regime): {
            bucket.label: bucket.as_dict()
            for bucket in build_buckets(frame[regimes == regime], config)
        }
        for regime in sorted(regimes.unique())
    }


def sufficiency_note(breakdown: Breakdown, config: EvaluationConfig | None = None) -> str:
    """One sentence a report can print about what this split can support."""
    config = config or EvaluationConfig()
    reportable = len(breakdown.reportable())
    thin = breakdown.thin()
    if not breakdown.groups:
        return f"No {breakdown.dimension} groups in this population."
    if not reportable:
        return (
            f"No {breakdown.dimension} group cleared the "
            f"{int(config.thresholds.min_regime_sample.value)}-setup floor "
            f"({len(thin)} group(s) present, largest {max(thin.values())}). "
            "Nothing regime-specific can be claimed from this population."
        )
    return (
        f"{reportable} of {len(breakdown.groups)} {breakdown.dimension} group(s) cleared "
        f"the sample floor; {len(thin)} reported a count only."
    )


def _evidence_scope(frame: pd.DataFrame) -> pd.Series:
    """CROSS_ASSET_ONLY / SAME_ASSET_SUPPORTED per row, vectorized.

    `groupby.cumcount()` over securities ordered by detection gives each
    setup its index within its own security's sequence in one pass — no
    per-row lookback, which at 10,000 securities is the difference
    between a groupby and a nested loop.
    """
    ordered = frame.sort_values(["security_id", "detected_at"])
    prior = ordered.groupby("security_id", sort=False).cumcount()
    scope = pd.Series(
        [
            SAME_ASSET_SUPPORTED if count >= SAME_ASSET_MINIMUM else CROSS_ASSET_ONLY
            for count in prior
        ],
        index=ordered.index,
    )
    return scope.reindex(frame.index)


__all__ = [
    "CROSS_ASSET_ONLY",
    "SAME_ASSET_MINIMUM",
    "SAME_ASSET_SUPPORTED",
    "Breakdown",
    "SampleSufficiency",
    "by_evidence_scope",
    "by_regime",
    "by_regime_and_bucket",
    "sufficiency_note",
]
