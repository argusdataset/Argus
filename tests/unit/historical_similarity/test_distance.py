"""Does the metric rank the right things as similar, and can it explain why?

The substantive question for a similarity engine: given a candidate that
looks like the MLSS/SLS/HIVE profile — deep decline, quiet compressed
base, thin range — do historical cases of that shape rank above an
unrelated one?

Fixtures are built from named structural profiles rather than random
noise, so a failure says something about the metric rather than about a
random seed. The shapes are the ones the project has been reasoning about
throughout: a deep microcap base, a shallow large-cap pullback, a
momentum name at its highs, and a collapsing security still falling.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.historical_similarity.config import SimilarityThresholds
from core.historical_similarity.distance import (
    estimate_scales,
    explain,
    pairwise_distances,
)
from core.historical_similarity.features import metric_features

#: Structural profiles, in the features the metric actually compares.
#: Only the discriminating features are pinned; the rest are filled with a
#: shared neutral value so any ranking difference is attributable to the
#: named features rather than to fixture noise.
PROFILES: dict[str, dict[str, float]] = {
    # The ARGUS target shape: deep decline into a tight, quiet base.
    "deep_microcap_base": {
        "drawdown_pct": -0.72,
        "peak_to_trough_decline": -0.78,
        "volatility_compression": 0.35,
        "atr_percentile": 0.05,
        "normalized_range_width": 0.06,
        "volume_contraction": 0.42,
        "support_test_count": 6.0,
    },
    # Same shape, slightly different depth — should be the nearest match.
    "deep_microcap_base_variant": {
        "drawdown_pct": -0.68,
        "peak_to_trough_decline": -0.74,
        "volatility_compression": 0.38,
        "atr_percentile": 0.07,
        "normalized_range_width": 0.07,
        "volume_contraction": 0.45,
        "support_test_count": 5.0,
    },
    # A mild pullback in a liquid name: same *direction*, far less extreme.
    "shallow_pullback": {
        "drawdown_pct": -0.12,
        "peak_to_trough_decline": -0.15,
        "volatility_compression": 0.90,
        "atr_percentile": 0.45,
        "normalized_range_width": 0.18,
        "volume_contraction": 0.95,
        "support_test_count": 1.0,
    },
    # At its highs and expanding — the opposite structure.
    "momentum_at_highs": {
        "drawdown_pct": -0.01,
        "peak_to_trough_decline": -0.04,
        "volatility_compression": 1.80,
        "atr_percentile": 0.95,
        "normalized_range_width": 0.35,
        "volume_contraction": 1.70,
        "support_test_count": 0.0,
    },
    # Still falling hard: deep like the base, but nothing has settled.
    "still_collapsing": {
        "drawdown_pct": -0.70,
        "peak_to_trough_decline": -0.75,
        "volatility_compression": 1.95,
        "atr_percentile": 0.92,
        "normalized_range_width": 0.55,
        "volume_contraction": 1.85,
        "support_test_count": 0.0,
    },
}

NEUTRAL = 0.0


def _vector(profile: str) -> pd.Series:
    values = dict.fromkeys(metric_features(), NEUTRAL)
    values.update(PROFILES[profile])
    return pd.Series(values, dtype=float)


@pytest.fixture(scope="module")
def cases() -> pd.DataFrame:
    """Every profile except the query itself, as a case set."""
    names = [n for n in PROFILES if n != "deep_microcap_base"]
    return pd.DataFrame([_vector(name) for name in names], index=names)


@pytest.fixture(scope="module")
def ranked(cases: pd.DataFrame) -> pd.DataFrame:
    candidate = _vector("deep_microcap_base")
    scales = estimate_scales(cases)
    return pairwise_distances(candidate, cases, scales).sort_values("distance")


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------


def test_the_matching_shape_ranks_first(ranked):
    """The central claim. A base resembles other bases."""
    assert ranked.index[0] == "deep_microcap_base_variant"


def test_the_opposite_structure_ranks_last(ranked):
    """A momentum name at its highs is the least like a deep quiet base."""
    assert ranked.index[-1] in {"momentum_at_highs", "still_collapsing"}


def test_a_deep_decline_alone_is_not_enough_to_rank_close(ranked):
    """`still_collapsing` has the same drawdown and must not rank as a twin.

    The discriminating test. A metric that keyed on decline depth would
    call a collapsing security a match for a quiet base, since both are
    70% off their highs. It is the volatility and range features that
    separate them, and this asserts they do.
    """
    order = list(ranked.index)
    assert order.index("still_collapsing") > order.index("deep_microcap_base_variant")


def test_distances_are_ordered_and_finite(ranked):
    distances = ranked["distance"].to_numpy()
    assert np.all(np.isfinite(distances))
    assert np.all(np.diff(distances) >= 0)


def test_a_vector_is_at_zero_distance_from_itself(cases):
    candidate = _vector("shallow_pullback")
    scales = estimate_scales(cases)
    distance = pairwise_distances(candidate, cases, scales)["distance"]["shallow_pullback"]
    assert distance == pytest.approx(0.0, abs=1e-12)


# --------------------------------------------------------------------------
# Explainability — the binding MVP constraint
# --------------------------------------------------------------------------


def test_a_match_decomposes_into_named_features(cases):
    """ "Why is this similar" must be answerable by naming features."""
    candidate = _vector("deep_microcap_base")
    scales = estimate_scales(cases)
    contributions = explain(candidate, cases.loc["deep_microcap_base_variant"], scales)

    assert contributions
    assert all(c.feature in set(metric_features()) for c in contributions)
    assert all(hasattr(c, "candidate_value") and hasattr(c, "case_value") for c in contributions)


def test_the_contribution_shares_sum_to_one_across_all_features(cases):
    """The decomposition is exact, not illustrative.

    That exactness is what makes it an explanation: the shares are the
    distance, re-expressed, rather than a plausible-looking gloss on it.
    """
    candidate = _vector("deep_microcap_base")
    scales = estimate_scales(cases)
    everything = explain(
        candidate, cases.loc["momentum_at_highs"], scales, top=len(metric_features())
    )
    assert sum(c.share for c in everything) == pytest.approx(1.0)


def test_contributions_are_ordered_closest_first(cases):
    """The user's question is which features made it close, not which differed."""
    candidate = _vector("deep_microcap_base")
    scales = estimate_scales(cases)
    contributions = explain(candidate, cases.loc["shallow_pullback"], scales, top=10)

    shares = [c.share for c in contributions]
    assert shares == sorted(shares)


# --------------------------------------------------------------------------
# Scaling and robustness
# --------------------------------------------------------------------------


def test_scales_come_from_the_cases_not_the_candidate(cases):
    """Otherwise the same pair would be more or less similar depending on
    which of the two was asked about."""
    scales = estimate_scales(cases)
    assert scales.sample_size == len(cases)

    a, b = _vector("deep_microcap_base"), _vector("shallow_pullback")
    forward = pairwise_distances(a, pd.DataFrame([b], index=["b"]), scales)["distance"]["b"]
    backward = pairwise_distances(b, pd.DataFrame([a], index=["a"]), scales)["distance"]["a"]
    assert forward == pytest.approx(backward)


def test_a_constant_feature_does_not_blow_up_the_metric():
    """A zero-IQR column must not divide by ~0 and swamp every distance."""
    columns = list(metric_features())
    frame = pd.DataFrame([dict.fromkeys(columns, 1.0) for _ in range(5)], index=range(5))
    scales = estimate_scales(frame)

    candidate = pd.Series(dict.fromkeys(columns, 2.0), dtype=float)
    distances = pairwise_distances(candidate, frame, scales)["distance"]

    assert np.all(np.isfinite(distances.to_numpy()))
    assert distances.iloc[0] == pytest.approx(1.0)


def test_median_absolute_deviation_resists_an_outlier():
    """Why MAD rather than standard deviation, demonstrated.

    One extreme case must not rescale the whole feature and compress every
    other distance toward zero — and with a small case set, extreme cases
    are guaranteed.
    """
    columns = list(metric_features())
    ordinary = pd.DataFrame(
        [dict(dict.fromkeys(columns, 0.0), drawdown_pct=v) for v in (-0.1, -0.2, -0.3, -0.4)],
        index=range(4),
    )
    with_outlier = pd.concat(
        [ordinary, pd.DataFrame([dict(dict.fromkeys(columns, 0.0), drawdown_pct=-50.0)], index=[9])]
    )

    assert estimate_scales(with_outlier).scales["drawdown_pct"] == pytest.approx(
        estimate_scales(ordinary).scales["drawdown_pct"], rel=0.5
    )


# --------------------------------------------------------------------------
# Missing features
# --------------------------------------------------------------------------


def test_a_pair_with_too_little_overlap_is_incomparable_not_distant(cases):
    """ "Too different" and "not enough shared features" are different facts.

    Returning a distance from six coincidentally-shared features would
    look exactly like a real one, so the pair is reported incomparable and
    the overlap that disqualified it is carried.
    """
    candidate = _vector("deep_microcap_base")
    blinded = cases.copy()
    keep = list(metric_features())[:3]
    for column in blinded.columns:
        if column not in keep:
            blinded[column] = np.nan

    result = pairwise_distances(candidate, blinded, estimate_scales(cases))
    assert result["distance"].isna().all()
    assert (result["overlap"] < SimilarityThresholds().min_feature_overlap.value).all()


def test_partial_overlap_still_computes_when_above_the_floor(cases):
    """Averaging over shared features keeps partial vectors comparable."""
    candidate = _vector("deep_microcap_base")
    partial = cases.copy()
    drop = list(metric_features())[:8]  # ~20% of 41
    partial[drop] = np.nan

    result = pairwise_distances(candidate, partial, estimate_scales(cases))
    assert result["distance"].notna().all()
    assert (result["shared_features"] < len(metric_features())).all()
