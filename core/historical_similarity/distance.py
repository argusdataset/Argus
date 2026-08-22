"""The distance metric: robust-scaled Euclidean, and why not something cleverer.

## The choice

**Mean Euclidean distance over robust-scaled features**, where each
feature is divided by the median absolute deviation of that feature
*across the historical case set*, and the sum is averaged over the
features the two vectors actually share.

Stated as a formula, for candidate `c` and case `h` over shared features
`S`:

```
distance(c, h) = sqrt( (1/|S|) * Σ_{f ∈ S} ((c_f - h_f) / scale_f)² )
```

## Why this and not the alternatives

**Not a learned embedding.** Ruled out by the MVP scope, and rightly: the
binding requirement is that "why is this similar to these 84 cases" be
answerable by naming features that are close in value. An embedding
answers "because the model says so", and there is no dataset to train one
on regardless.

**Not Mahalanobis**, despite it being the textbook answer for correlated
features. Mahalanobis needs a covariance matrix estimated from the case
set, and with 41 features it needs *far* more cases than features before
that estimate is anything but noise — the usual guidance is several times
`p`, so several hundred cases minimum. The historical dataset is nearly
empty until Module 17 runs. A Mahalanobis distance computed from a
near-singular covariance estimate would be numerically unstable and
confidently wrong, which is worse than a simpler metric that is honestly
approximate. It is also far harder to explain: the contribution of one
feature depends on every other, so "these features are close" stops being
a decomposable statement.

**Not raw Euclidean.** Module 08's features live on wildly different
scales — `drawdown_pct` spans about [-1, 0] while `support_test_count`
counts events and can reach 20. Unscaled, the count features would
dominate entirely.

**Interquartile range rather than standard deviation**, because the case
set is small and will contain genuine outliers (a security that fell
95%). A standard deviation is dragged around by exactly those cases; the
IQR discards the tails.

**And rather than median absolute deviation**, which was the first
choice and was wrong. MAD is robust to outliers but *blind to
bimodality*: it measures the spread of the majority cluster and ignores
the gap between clusters entirely. A real case set is exactly
multi-modal — it contains different setup archetypes — and with a
population of three base-shaped and four momentum-shaped cases, every
MAD collapsed to the within-cluster noise (0.03) while the between-shape
gap was over a hundred times larger. Distances inflated to ~900 against
a radius of 1.25, and every case looked equally remote.

The IQR spans the 25th to 75th percentile, so a genuine split in the
population lands inside it. It keeps the outlier robustness that
motivated MAD and drops the blindness.

## What makes it explainable

`FeatureContribution` is returned per feature per match, so a match
decomposes into "these five features account for most of the closeness".
That decomposition is arithmetic, not interpretation — the squared terms
literally sum to the distance.

## A property worth knowing: averaging dilutes

Taking the *mean* over shared features rather than the sum is what makes
pairs with different overlap comparable. It also means that when two
vectors agree on most features and disagree sharply on a few, the
disagreement is diluted by the agreement.

That is the correct trade for handling missing data, but it has a
consequence a consumer should know: **the metric measures overall
structural resemblance, not disagreement on any particular axis.** Two
setups that match everywhere except being at opposite ends of their
ranges will read as fairly similar. If a downstream module needs "close
on these specific features", it should read the per-feature
contributions rather than thresholding the distance.

Measured during development: with only seven features differing out of
41 and the rest identical, an unrelated momentum name sat at distance
0.84 against a 1.25 radius. With realistically varied vectors the same
comparison is 1.68. The radius is only meaningful against vectors that
vary the way real ones do.

## Missing features

Distances are computed over the *intersection* of features present in
both vectors, and the mean (not the sum) is taken, so a pair sharing 20
features is comparable with a pair sharing 40. Below
`min_feature_overlap` the pair is reported incomparable rather than
assigned a distance from whatever few features happened to align — a
number derived from six coincidentally-shared features would look exactly
like a real one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.historical_similarity.config import SimilarityThresholds
from core.historical_similarity.features import metric_features


@dataclass(frozen=True, slots=True)
class FeatureContribution:
    """One feature's share of a single distance."""

    feature: str
    candidate_value: float
    case_value: float
    scaled_difference: float
    #: Fraction of the total squared distance this feature accounts for.
    share: float


@dataclass(frozen=True, slots=True)
class FeatureScales:
    """Per-feature robust scales, estimated from the historical case set.

    Estimated from the *cases*, not from the candidate, because the scale
    has to be a property of the reference population. Deriving it from the
    query would make the same two setups more or less similar depending on
    which was asked about.
    """

    scales: dict[str, float]
    #: How many cases the estimate came from — small numbers make every
    #: scale here shaky, which the caller should know.
    sample_size: int

    def get(self, feature: str, floor: float) -> float:
        return max(self.scales.get(feature, 1.0), floor)


def estimate_scales(
    cases: pd.DataFrame, thresholds: SimilarityThresholds | None = None
) -> FeatureScales:
    """Interquartile range per feature, floored.

    Falls back to 1.0 for a feature whose IQR is zero or unmeasurable
    (constant across the case set, or absent from it). 1.0 leaves that
    feature's raw units in play rather than dividing by a floor near zero,
    which would let a constant feature swamp the metric the instant one
    case differed.

    See the module docstring on why IQR rather than median absolute
    deviation — MAD's blindness to a multi-modal population was found by
    testing, not by inspection.
    """
    thresholds = thresholds or SimilarityThresholds()
    floor = thresholds.scale_floor.value
    scales: dict[str, float] = {}

    for feature in metric_features():
        if feature not in cases.columns:
            scales[feature] = 1.0
            continue
        column = cases[feature].dropna()
        if column.empty:
            scales[feature] = 1.0
            continue
        spread = float(column.quantile(0.75) - column.quantile(0.25))
        scales[feature] = spread if spread > floor else 1.0

    return FeatureScales(scales=scales, sample_size=len(cases))


def pairwise_distances(
    candidate: pd.Series,
    cases: pd.DataFrame,
    scales: FeatureScales,
    thresholds: SimilarityThresholds | None = None,
) -> pd.DataFrame:
    """Distance from one candidate to every case, vectorized.

    Returns a frame indexed like `cases`, with `distance`, `shared_features`
    and `overlap` columns. Incomparable pairs carry `NaN` distance and the
    overlap that disqualified them, so a caller can tell "too different"
    from "not enough shared features to say" — a distinction that
    disappears if both come back as simply absent.
    """
    thresholds = thresholds or SimilarityThresholds()
    features = [f for f in metric_features() if f in cases.columns]
    if not features or cases.empty:
        return pd.DataFrame(
            {"distance": [], "shared_features": [], "overlap": []}, index=cases.index
        )

    case_values = cases[features].to_numpy(dtype=float)
    candidate_values = candidate.reindex(features).to_numpy(dtype=float)
    scale_vector = np.array(
        [scales.get(f, thresholds.scale_floor.value) for f in features], dtype=float
    )

    both_present = ~np.isnan(case_values) & ~np.isnan(candidate_values)[None, :]
    scaled = (case_values - candidate_values[None, :]) / scale_vector[None, :]
    squared = np.where(both_present, scaled**2, 0.0)

    shared = both_present.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        distance = np.sqrt(squared.sum(axis=1) / shared)

    overlap = shared / len(metric_features())
    distance = np.where(overlap >= thresholds.min_feature_overlap.value, distance, np.nan)

    return pd.DataFrame(
        {"distance": distance, "shared_features": shared, "overlap": overlap},
        index=cases.index,
    )


def explain(
    candidate: pd.Series,
    case: pd.Series,
    scales: FeatureScales,
    thresholds: SimilarityThresholds | None = None,
    *,
    top: int = 5,
) -> tuple[FeatureContribution, ...]:
    """Decompose one distance into per-feature contributions.

    The answer to "why is this considered similar". Ordered by share
    ascending, so the features that made the pair *close* come first —
    which is the question a user asks, rather than which features differed
    most.
    """
    thresholds = thresholds or SimilarityThresholds()
    contributions: list[FeatureContribution] = []
    total = 0.0

    for feature in metric_features():
        candidate_value = candidate.get(feature)
        case_value = case.get(feature)
        if candidate_value is None or case_value is None:
            continue
        if pd.isna(candidate_value) or pd.isna(case_value):
            continue
        scaled = (float(case_value) - float(candidate_value)) / scales.get(
            feature, thresholds.scale_floor.value
        )
        total += scaled**2
        contributions.append(
            FeatureContribution(
                feature=feature,
                candidate_value=float(candidate_value),
                case_value=float(case_value),
                scaled_difference=scaled,
                share=0.0,
            )
        )

    if not contributions:
        return ()

    # Shares sum to 1 by construction — the decomposition is exact, which
    # is what makes it an explanation rather than an illustration.
    divisor = total if total > 0 else 1.0
    ranked = sorted(
        (
            FeatureContribution(
                feature=c.feature,
                candidate_value=c.candidate_value,
                case_value=c.case_value,
                scaled_difference=c.scaled_difference,
                share=(c.scaled_difference**2) / divisor,
            )
            for c in contributions
        ),
        key=lambda c: c.share,
    )
    return tuple(ranked[:top])
