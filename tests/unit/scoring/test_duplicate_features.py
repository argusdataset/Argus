"""Module 08's duplicate feature: proven real, and handled visibly.

Module 11 flagged that `volatility_compression` (Group B) and
`volatility_contraction_onset` (Group A) are the same signal under two
names. The Module 13 brief says silence is not an option: either fix it at
the source or make sure it cannot double-count here. Fixing it at the
source is Module 08's call — a feature rename invalidates every stored
`feature_schema_version` — so this module does the second, and this file
is what makes "does not double-count" checkable instead of asserted.

The first test computes both features from real bars and asserts they are
numerically identical. Without it, the exclusion below would be defending
against a duplication that might have been fixed upstream, and nobody
would notice when the defence stopped being needed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.feature_engine.groups import consolidation, decline
from core.feature_engine.panel import PricePanel
from core.feature_engine.spec import FeatureSpec
from core.scoring.components import (
    COMPONENT_FEATURES,
    DUPLICATE_FEATURES,
    TARGET_MODEL_INTERNAL_FEATURES,
    VOLATILITY_STRUCTURE,
)


@pytest.fixture
def panel() -> PricePanel:
    """A year of bars for two securities, with enough variation that two
    genuinely different volatility measures would diverge."""
    dates = pd.date_range("2023-01-02", periods=400, freq="B", tz="UTC")
    generator = np.random.default_rng(13)
    frames = {}
    for name in ("open_adj", "high_adj", "low_adj", "close_adj", "close_raw", "volume"):
        frames[name] = pd.DataFrame(index=dates, columns=["A", "B"], dtype=float)

    for column in ("A", "B"):
        steps = generator.normal(0.0, 0.02, size=len(dates))
        # A regime change halfway through, so a compression ratio and its
        # duplicate have something to disagree about if they can.
        steps[len(dates) // 2 :] *= 4.0
        close = 50.0 * np.exp(np.cumsum(steps))
        frames["close_adj"][column] = close
        frames["open_adj"][column] = close
        frames["close_raw"][column] = close
        frames["high_adj"][column] = close * 1.01
        frames["low_adj"][column] = close * 0.99
        frames["volume"][column] = generator.integers(100_000, 500_000, len(dates))

    return PricePanel(**frames)


def test_the_two_features_really_are_the_same_signal(panel):
    """The premise of the exclusion, verified against real arithmetic.

    If Module 08 ever makes them genuinely different this fails, and the
    exclusion in `volatility_structure` should be revisited rather than
    left in place out of habit.
    """
    spec = FeatureSpec()
    group_a = decline.compute(panel, spec)["volatility_contraction_onset"]
    group_b = consolidation.compute(panel, spec)["volatility_compression"]

    pd.testing.assert_frame_equal(group_a, group_b, check_names=False)
    assert group_a.notna().to_numpy().sum() > 0, "fixture must produce real readings"


def test_the_suppressed_name_is_the_one_that_is_not_read():
    """The mapping says which name loses, and it is unambiguous."""
    assert DUPLICATE_FEATURES == {"volatility_contraction_onset": "volatility_compression"}


@pytest.mark.parametrize("component", sorted(COMPONENT_FEATURES))
def test_no_component_reads_a_suppressed_feature(component):
    """The load-bearing check.

    Declared input lists rather than a reading of the code, so adding
    `volatility_contraction_onset` to any component fails here rather than
    quietly giving one signal two thirds of a component's weight.
    """
    read = set(COMPONENT_FEATURES[component])
    assert not read & set(DUPLICATE_FEATURES), (
        f"{component} reads a suppressed duplicate. See DUPLICATE_FEATURES."
    )


def test_no_component_reads_both_halves_of_any_duplicate_pair():
    """The general form: even if the mapping grew, no component may hold
    both names of a pair."""
    for read in COMPONENT_FEATURES.values():
        for suppressed, kept in DUPLICATE_FEATURES.items():
            assert not ({suppressed, kept} <= set(read))


def test_volatility_structure_reads_three_distinct_signals(panel):
    """The component claims three independent readings; this asserts they
    are three, not two plus a copy."""
    read = COMPONENT_FEATURES[VOLATILITY_STRUCTURE]
    spec = FeatureSpec()
    computed = consolidation.compute(panel, spec)

    assert len(set(read)) == len(read) == 3
    frames = [computed[name] for name in read]
    for first in range(len(frames)):
        for second in range(first + 1, len(frames)):
            assert not frames[first].equals(frames[second])


def test_the_target_model_no_longer_reads_both_names():
    """The Module 13 finding, now fixed upstream and guarded from here.

    target-model-v1 used to read `volatility_contraction_onset` in its
    `stabilization` sub-component and `volatility_compression` in its
    `consolidation` sub-component, so one measurement entered its
    `quality` twice — and that quality is the largest single share of
    `argus_score`. Module 14 removed the stabilization reading.

    Asserted from here as well as from Module 10's own tests because this
    is the module that has to live with the consequence: nothing else
    would notice if the suppressed name crept back into the model.
    """
    from core.market_state.target_model_matching.models.target_model_v1.model import (
        COMPONENT_INPUTS,
    )

    reads = {name for names in COMPONENT_INPUTS.values() for name in names}

    assert "volatility_contraction_onset" not in reads
    assert "volatility_compression" in reads
    assert reads <= set(TARGET_MODEL_INTERNAL_FEATURES)
    # And no feature is read by two sub-components either, which is the
    # general form of the same mistake.
    read_lists = [name for names in COMPONENT_INPUTS.values() for name in names]
    assert len(read_lists) == len(set(read_lists))


def test_the_two_modules_agree_on_which_name_is_suppressed():
    """Two independent declarations of one fact drift silently. This is
    what stops them."""
    from core.market_state.target_model_matching.models.target_model_v1.model import (
        SUPPRESSED_INPUTS,
    )

    assert SUPPRESSED_INPUTS == DUPLICATE_FEATURES
