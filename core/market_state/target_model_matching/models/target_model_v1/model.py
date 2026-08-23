"""target-model-v1: Long Decline → Stabilization → Consolidation → Awakening.

The specific pattern ARGUS was built to find, expressed as a match score
over Module 08's features. Four components, one per phase, combined by
the weights in `thresholds.py`.

## What this judges, and what it does not

It judges **quality of match**, on securities the state engine has already
placed in one of four states. It does not decide which state a security is
in, and it cannot veto a state assignment. See
`target_model_matching/interface.py` for why that boundary is drawn where
it is — and for the genuine ambiguity in the brief about whether it should
be.

## Why saturation instead of thresholds

Each component saturates rather than stepping. A 60% decline and an 80%
decline both score 1.0 on the decline component, because past some depth
the difference stops carrying information about whether this is the setup.
Between zero and saturation the response is linear.

The alternative — a step at some depth — would reintroduce exactly the
brittleness Module 08's "no fixed thresholds" rule exists to prevent: a
security at 29% would score nothing and one at 31% would score everything,
which is not a distinction the data supports.

## Vectorized

Every component is computed over the whole (securities x features) frame
at once. The only per-security work is assembling the result objects,
matching the arrangement Modules 08 and 09 use.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import numpy as np
import pandas as pd

from core.market_state.target_model_matching.interface import TargetModelAssessment
from core.market_state.target_model_matching.models.target_model_v1.thresholds import (
    TargetModelV1Thresholds,
)
from infra.db.enums import MarketState

#: The slice of the state machine this model judges.
COVERED_STATES: frozenset[MarketState] = frozenset(
    {
        MarketState.CONSOLIDATION,
        MarketState.ACCUMULATION,
        MarketState.BREAKOUT_WATCH,
        MarketState.BREAKOUT_READY,
    }
)

#: Features each component reads. Named here so `unavailable_inputs` can
#: report precisely what was missing rather than "something".
COMPONENT_INPUTS: dict[str, tuple[str, ...]] = {
    "prior_decline": ("peak_to_trough_decline",),
    "stabilization": ("downside_momentum_reduction",),
    "consolidation": ("volatility_compression", "normalized_range_width"),
    "awakening": ("volume_expansion", "resistance_pressure"),
}

#: Module 08 computes `volatility_contraction_onset` (Group A) and
#: `volatility_compression` (Group B) from identical arithmetic under two
#: names. Module 11 found the duplication; Module 13 found what it was
#: doing here — this model used to read the Group A name in
#: `stabilization` and the Group B name in `consolidation`, so one
#: measurement entered `quality` twice and inflated the largest single
#: share of `argus_score`.
#:
#: The key is the name this model refuses to read, the value is the one it
#: reads instead — the same shape as Module 13's `DUPLICATE_FEATURES`, and
#: there is a test asserting the two modules agree.
#:
#: **Consolidation keeps it, stabilization loses it.** Coiling *is*
#: volatility compression — that is what the phase means, and the
#: consolidation component has a dedicated `compression_saturation`
#: threshold tuned for it. Stabilization's own signal is new lows becoming
#: rarer; its volatility reading was borrowed from a Group A feature that
#: turned out to be the Group B one wearing a different name.
#:
#: Not fixed at Module 08's source, for the reason Module 13 gave:
#: renaming a feature invalidates every stored `feature_schema_version`
#: checksum.
SUPPRESSED_INPUTS: dict[str, str] = {
    "volatility_contraction_onset": "volatility_compression",
}

MODEL_INPUTS: tuple[str, ...] = tuple(
    dict.fromkeys(name for names in COMPONENT_INPUTS.values() for name in names)
)


class TargetModelV1:
    """The Long Decline → Expansion pattern, as a match scorer."""

    name = "target-model-v1"

    def __init__(self, thresholds: TargetModelV1Thresholds | None = None) -> None:
        self.thresholds = thresholds or TargetModelV1Thresholds()

    def covered_states(self) -> frozenset[MarketState]:
        return COVERED_STATES

    def required_features(self) -> tuple[str, ...]:
        return MODEL_INPUTS

    def assess(
        self,
        features: pd.DataFrame,
        states: pd.Series,
        as_of: datetime,
    ) -> dict[UUID, TargetModelAssessment]:
        """Assess every security the engine placed in a covered state."""
        covered = states[states.isin(COVERED_STATES)]
        if covered.empty or features.empty:
            return {}

        frame = features.reindex(covered.index)
        components = self._components(frame)
        quality, coverage = self._combine(components, frame)

        assessments: dict[UUID, TargetModelAssessment] = {}
        for security_id in covered.index:
            row_coverage = float(coverage.get(security_id, 0.0))
            missing = tuple(name for name in MODEL_INPUTS if pd.isna(frame.at[security_id, name]))
            if row_coverage < self.thresholds.minimum_input_coverage:
                assessments[security_id] = TargetModelAssessment(
                    security_id=security_id,
                    quality=None,
                    unavailable_inputs=missing,
                )
                continue

            value = quality.get(security_id)
            score = None if value is None or pd.isna(value) else float(value)
            assessments[security_id] = TargetModelAssessment(
                security_id=security_id,
                quality=score,
                components={
                    name: float(series[security_id])
                    for name, series in components.items()
                    if not pd.isna(series[security_id])
                },
                supports_advancement=(
                    score is not None and score >= self.thresholds.advancement_quality
                ),
                unavailable_inputs=missing,
            )
        return assessments

    # ------------------------------------------------------------------
    # Components — each 0..1, each vectorized over the whole frame
    # ------------------------------------------------------------------

    def _components(self, frame: pd.DataFrame) -> dict[str, pd.Series]:
        return {
            "prior_decline": self._prior_decline(frame),
            "stabilization": self._stabilization(frame),
            "consolidation": self._consolidation(frame),
            "awakening": self._awakening(frame),
        }

    def _prior_decline(self, frame: pd.DataFrame) -> pd.Series:
        """Depth of the fall, saturating. Deeper is a better match, to a point."""
        depth = -frame["peak_to_trough_decline"]
        return _saturate(depth, self.thresholds.decline_saturation)

    def _stabilization(self, frame: pd.DataFrame) -> pd.Series:
        """The decline losing force: new lows becoming rarer.

        One reading, not two. This component used to average in a
        contracting-volatility term computed from
        `volatility_contraction_onset` — which is the same series
        `_consolidation` reads as `volatility_compression`, so the pair
        double-counted one measurement into `quality`. See
        `SUPPRESSED_INPUTS`.
        """
        return _saturate(frame["downside_momentum_reduction"], 1.0)

    def _consolidation(self, frame: pd.DataFrame) -> pd.Series:
        """Coiling: volatility compressed, range tight relative to price."""
        compressed = _saturate(
            1.0 - frame["volatility_compression"], 1.0 - self.thresholds.compression_saturation
        )
        tight = _saturate(1.0 - frame["normalized_range_width"], 1.0)
        return _mean_available([compressed, tight])

    def _awakening(self, frame: pd.DataFrame) -> pd.Series:
        """Stirring: volume returning, price pressing the top of the range."""
        volume = _saturate(
            frame["volume_expansion"] - 1.0, self.thresholds.awakening_saturation - 1.0
        )
        pressure = _saturate(frame["resistance_pressure"], 1.0)
        return _mean_available([volume, pressure])

    def _combine(
        self, components: dict[str, pd.Series], frame: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """Weighted mean over the components that computed, plus input coverage.

        Renormalizing over available components rather than treating an
        absent one as zero: a missing input is not evidence of a poor
        match, and scoring it as one would penalize thin data twice —
        Module 09's gates already adjudicate data sufficiency.
        """
        weights = self.thresholds.weights()
        weighted = pd.DataFrame(
            {name: components[name] * weights[name] for name in components},
            index=frame.index,
        )
        applied = pd.DataFrame(
            {name: components[name].notna() * weights[name] for name in components},
            index=frame.index,
        )
        total_weight = applied.sum(axis=1).replace(0.0, np.nan)
        quality = weighted.sum(axis=1, skipna=True) / total_weight

        present = frame[list(MODEL_INPUTS)].notna().sum(axis=1)
        coverage = present / len(MODEL_INPUTS)
        return quality, coverage


def _saturate(series: pd.Series, ceiling: float) -> pd.Series:
    """Scale to 0..1, saturating at `ceiling`. NaN stays NaN.

    A ceiling of zero or less would make every finite value saturate, so
    it is refused rather than silently producing an all-ones column.
    """
    if ceiling <= 0:
        raise ValueError(f"saturation ceiling must be positive, got {ceiling}")
    return (series / ceiling).clip(lower=0.0, upper=1.0)


def _mean_available(parts: list[pd.Series]) -> pd.Series:
    """Mean across parts, ignoring absent ones; NaN only when all are absent."""
    return pd.concat(parts, axis=1).mean(axis=1, skipna=True)
