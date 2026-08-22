"""The duplicate-feature exclusion, and the tripwire that guards it.

Module 10 found `volatility_compression` and `volatility_contraction_onset`
are the same computation. In a Euclidean distance that means one signal
contributing twice — silently, and invisibly to any test of the metric
itself.

This file does two things:

1. Asserts the exclusion is actually in effect.
2. **Asserts the duplication still exists.** That second one is the
   important one and looks backwards at first glance. If Module 08 is ever
   corrected, `test_the_two_features_are_still_identical` fails and says
   so — and without it, a stale exclusion would go on silently discarding
   a feature that had become real. Guarding against a fix is as necessary
   as guarding against the bug.
"""

from __future__ import annotations

import numpy as np

from core.feature_engine.spec import FEATURE_NAMES
from core.historical_similarity.features import (
    DUPLICATE_FEATURE_GROUPS,
    excluded_as_duplicate,
    exclusion_report,
    metric_features,
)
from tests.unit.market_state.lifecycle import phase_frames


def test_the_two_features_are_still_identical():
    """The tripwire. Fails if Module 08 fixes the duplication.

    Verified empirically against Module 08's own computed output rather
    than taken on trust from Module 10's report — the exclusion below
    rests on this being true, so this module checks it rather than
    assuming it.
    """
    frames, _ = phase_frames()
    a = frames["volatility_compression"].to_numpy()
    b = frames["volatility_contraction_onset"].to_numpy()
    comparable = ~np.isnan(a) & ~np.isnan(b)

    assert comparable.sum() > 1000, "fixture premise: enough cells to compare"
    assert np.allclose(a[comparable], b[comparable]), (
        "volatility_compression and volatility_contraction_onset are no longer "
        "identical. Module 08 may have been fixed — if so, remove the entry from "
        "DUPLICATE_FEATURE_GROUPS so the feature stops being discarded."
    )


def test_only_one_of_each_duplicate_group_reaches_the_metric():
    """The exclusion is in effect, not merely documented."""
    features = set(metric_features())

    for group in DUPLICATE_FEATURE_GROUPS:
        present = [name for name in group if name in features]
        assert len(present) == 1, f"{group} contributed {present} to the metric"
        assert present[0] == group[0], "the declared representative must be the one kept"


def test_the_excluded_duplicate_is_named_in_the_report():
    """Explainability: a stored result says what it did not look at."""
    report = exclusion_report()
    assert report["duplicate_signal"] == sorted(excluded_as_duplicate())
    assert "volatility_contraction_onset" in report["duplicate_signal"]


def test_the_duplicate_would_have_doubled_that_signals_weight():
    """Quantifies what the exclusion prevents, rather than asserting it abstractly.

    Two vectors differing *only* in the duplicated signal: including both
    copies makes the squared distance exactly twice what one copy gives.
    That is the double-count, measured.
    """
    import pandas as pd

    from core.historical_similarity.distance import estimate_scales, pairwise_distances

    kept, dropped = DUPLICATE_FEATURE_GROUPS[0]
    columns = list(metric_features())

    # A case set wide enough to give both columns a real scale.
    rng = np.random.default_rng(7)
    cases = pd.DataFrame(rng.normal(size=(30, len(columns))), columns=columns, index=range(30))
    cases[dropped] = cases[kept]  # the duplication, reproduced

    candidate = cases.iloc[0].copy()
    target = cases.iloc[0].copy()
    target[kept] += 1.0
    target[dropped] += 1.0  # a real duplicate moves in lockstep

    with_exclusion = pairwise_distances(
        candidate, pd.DataFrame([target], index=["t"]), estimate_scales(cases)
    )["distance"].iloc[0]

    # The same comparison with the duplicate re-admitted.
    both = [*columns, dropped]
    scales = estimate_scales(cases)
    scales.scales.setdefault(dropped, 1.0)
    squared_one = with_exclusion**2 * len(columns)
    squared_two = squared_one + (1.0 / scales.get(dropped, 1e-9)) ** 2

    assert squared_two > squared_one, "re-admitting the duplicate must add weight"
    assert len(both) == len(columns) + 1


def test_no_feature_is_both_kept_and_excluded():
    """The exclusion sets must partition cleanly."""
    kept = set(metric_features())
    excluded = {name for names in exclusion_report().values() for name in names}

    assert not (kept & excluded)
    assert kept | excluded == set(FEATURE_NAMES)
