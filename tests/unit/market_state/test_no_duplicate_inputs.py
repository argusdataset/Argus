"""The target model must read each underlying signal once.

Module 11 found that Module 08 computes `volatility_contraction_onset`
(Group A) and `volatility_compression` (Group B) from identical
arithmetic under two names. Module 13 found what that was doing here:
this model read the Group A name in `stabilization` and the Group B name
in `consolidation`, so one measurement entered `quality` twice — and
`quality` is the largest single share of `argus_score`.

Module 14 removed the stabilization reading. These tests are what stop it
coming back. The first proves the duplication is real by computing both
features from actual bars, so that if Module 08 ever makes them genuinely
different the exclusion is revisited rather than left in place out of
habit — the same discipline as Module 13's own duplicate test.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.feature_engine.groups import consolidation, decline
from core.feature_engine.panel import PricePanel
from core.feature_engine.spec import FeatureSpec
from core.market_state.target_model_matching.models.target_model_v1.model import (
    COMPONENT_INPUTS,
    MODEL_INPUTS,
    SUPPRESSED_INPUTS,
    TargetModelV1,
)


@pytest.fixture
def panel() -> PricePanel:
    dates = pd.date_range("2023-01-02", periods=400, freq="B", tz="UTC")
    generator = np.random.default_rng(14)
    frames = {
        name: pd.DataFrame(index=dates, columns=["A", "B"], dtype=float)
        for name in ("open_adj", "high_adj", "low_adj", "close_adj", "close_raw", "volume")
    }
    for column in ("A", "B"):
        steps = generator.normal(0.0, 0.02, size=len(dates))
        steps[len(dates) // 2 :] *= 4.0
        close = 50.0 * np.exp(np.cumsum(steps))
        for name in ("close_adj", "close_raw", "open_adj"):
            frames[name][column] = close
        frames["high_adj"][column] = close * 1.01
        frames["low_adj"][column] = close * 0.99
        frames["volume"][column] = generator.integers(100_000, 500_000, len(dates))
    return PricePanel(**frames)


def test_the_two_features_really_are_the_same_signal(panel):
    """The premise of the exclusion, verified against real arithmetic."""
    spec = FeatureSpec()
    group_a = decline.compute(panel, spec)["volatility_contraction_onset"]
    group_b = consolidation.compute(panel, spec)["volatility_compression"]

    pd.testing.assert_frame_equal(group_a, group_b, check_names=False)
    assert group_a.notna().to_numpy().sum() > 0, "fixture must produce real readings"


def test_the_model_does_not_read_the_suppressed_name():
    read = [name for names in COMPONENT_INPUTS.values() for name in names]

    assert "volatility_contraction_onset" not in read
    assert "volatility_compression" in read
    assert set(read) & set(SUPPRESSED_INPUTS) == set()


def test_no_feature_is_read_by_two_sub_components():
    """The general form of the same mistake.

    Two sub-components sharing an input means that input carries the sum
    of both their weights while appearing to be two independent readings
    — which is exactly what the duplicate did before it was named.
    """
    read = [name for names in COMPONENT_INPUTS.values() for name in names]

    assert len(read) == len(set(read))
    assert set(MODEL_INPUTS) == set(read)


def test_stabilization_no_longer_moves_when_the_compression_signal_moves(panel):
    """A behavioural check, not just a declaration.

    Two frames identical except for `volatility_compression`. The
    consolidation component must move; stabilization must not. Before the
    fix both moved, which is what double-counting looks like from the
    outside.
    """
    from uuid import uuid4

    security_id = uuid4()
    base = pd.DataFrame(
        {name: [0.5] for name in MODEL_INPUTS}, index=pd.Index([security_id])
    )
    base["peak_to_trough_decline"] = -0.4
    shifted = base.copy()
    shifted["volatility_compression"] = 0.95

    model = TargetModelV1()
    first = model._components(base)
    second = model._components(shifted)

    assert first["consolidation"][security_id] != second["consolidation"][security_id]
    assert first["stabilization"][security_id] == second["stabilization"][security_id]


def test_the_input_coverage_denominator_matches_what_the_model_reads():
    """Coverage is `present / len(MODEL_INPUTS)`. Leaving a feature in that
    tuple after no component reads it would quietly make every security
    look less covered than it is."""
    read = {name for names in COMPONENT_INPUTS.values() for name in names}

    assert len(MODEL_INPUTS) == len(read)
