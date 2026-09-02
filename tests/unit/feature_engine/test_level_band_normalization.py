"""The level-test band scales with volatility, so the counts it feeds do too.

`support_test_count` and `resistance_test_count` answer "how often did
price come back to the edge of its range" — a question about structure.
Measured against a flat percentage of price they answered a different
question as well, because price sits inside a fixed percentage window in
inverse proportion to how far it travels per session.

These tests construct two securities with the *identical* geometric
shape — same range, same number of visits to its edges, same relative
straddle — differing only in amplitude, and assert the counts agree. The
flat band is reproduced inline so the difference it made is visible
rather than asserted from memory.
"""

from __future__ import annotations

from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from core.feature_engine.groups import consolidation
from core.feature_engine.panel import PricePanel
from core.feature_engine.spec import FeatureSpec, FeatureTolerances

QUIET = uuid4()
VOLATILE = uuid4()

BARS = 120
PERIOD = 6
BASE_PRICE = 100.0

#: A quiet security whose whole range is narrower than the old 1.5% band,
#: and a violent one whose range is many times wider. Same shape, same
#: number of visits to the range floor; only the amplitude differs.
QUIET_AMPLITUDE = 1.0
VOLATILE_AMPLITUDE = 10.0

#: The flat fraction of price `level_test` used to be, reproduced here so
#: the comparison is against the real prior behaviour.
LEGACY_FLAT_BAND = 0.015


def _triangle(amplitude: float) -> np.ndarray:
    """A sawtooth that returns to its floor once per `PERIOD` bars."""
    position = np.arange(BARS) % PERIOD
    distance_from_middle = np.abs(position - PERIOD / 2) / (PERIOD / 2)
    return BASE_PRICE + amplitude * (1.0 - distance_from_middle)


@pytest.fixture(scope="module")
def panel() -> PricePanel:
    index = pd.bdate_range("2024-01-02", periods=BARS, tz="UTC")
    close = pd.DataFrame(
        {QUIET: _triangle(QUIET_AMPLITUDE), VOLATILE: _triangle(VOLATILE_AMPLITUDE)},
        index=index,
    )
    # A straddle proportional to each security's own amplitude, so the two
    # series are geometrically similar rather than merely similar in shape.
    straddle = pd.DataFrame(
        {
            QUIET: np.full(BARS, 0.05 * QUIET_AMPLITUDE),
            VOLATILE: np.full(BARS, 0.05 * VOLATILE_AMPLITUDE),
        },
        index=index,
    )
    return PricePanel(
        open_adj=close,
        high_adj=close + straddle,
        low_adj=close - straddle,
        close_adj=close,
        close_raw=close,
        volume=close * 0.0 + 1_000_000.0,
    )


def _legacy_support_count(panel: PricePanel, spec: FeatureSpec) -> pd.DataFrame:
    """`support_test_count` as the flat-percentage band computed it."""
    window = spec.windows.medium
    range_low = panel.low_adj.rolling(window).min()
    near = (panel.low_adj - range_low).abs() <= (panel.close_adj * LEGACY_FLAT_BAND)
    return near.astype(float).rolling(window).sum()


def test_the_flat_band_counted_volatility_as_well_as_structure(panel):
    """The prior behaviour, demonstrated rather than described.

    The quiet security's entire range is narrower than the old band, so
    every single bar registered as a support test — a saturated count
    that says nothing about structure. The violent one, with the same
    shape, registered a fraction of that.
    """
    legacy = _legacy_support_count(panel, FeatureSpec())

    quiet = legacy[QUIET].iloc[-1]
    volatile = legacy[VOLATILE].iloc[-1]

    assert quiet == FeatureSpec().windows.medium  # saturated: every bar
    assert volatile < quiet / 2


@pytest.mark.parametrize("count_name", ["support_test_count", "resistance_test_count"])
@pytest.mark.parametrize("multiple", [0.05, 0.25, 0.75, 1.0, 2.0])
def test_the_atr_band_counts_the_same_structure_the_same_way(panel, multiple, count_name):
    """The fix: identical shape, identical count, whatever the amplitude.

    Asserted across the whole plausible range of the multiple rather than
    at its default, because the property being claimed is that the band
    *normalizes* — not that one particular value happens to line up. A
    band that only agreed at 0.25 would be a coincidence, not a fix.
    """
    counts = consolidation.compute(
        panel, FeatureSpec(tolerances=FeatureTolerances(level_test_atr=multiple))
    )[count_name]

    assert counts[QUIET].iloc[-1] == counts[VOLATILE].iloc[-1]


def test_a_wider_band_admits_more_touches(panel):
    """A positive control.

    The tests above would also pass against a band that admitted nothing
    at all, or everything, for both securities equally. This shows the
    multiple is actually doing work.
    """
    narrow = consolidation.compute(
        panel, FeatureSpec(tolerances=FeatureTolerances(level_test_atr=0.10))
    )["support_test_count"]
    wide = consolidation.compute(
        panel, FeatureSpec(tolerances=FeatureTolerances(level_test_atr=2.0))
    )["support_test_count"]

    assert wide[QUIET].iloc[-1] > narrow[QUIET].iloc[-1]
    assert wide[VOLATILE].iloc[-1] > narrow[VOLATILE].iloc[-1]


def test_the_counts_are_absent_rather_than_zero_before_the_atr_exists(panel):
    """`NaN <= NaN` is False, not NaN.

    Without masking, the bars before the ATR window fills would enter the
    rolling sum as measured non-tests — a fabricated zero standing in for
    an absent measurement, which is the one thing Module 08 refuses to do.
    """
    counts = consolidation.compute(panel, FeatureSpec())["support_test_count"]
    window = FeatureSpec().windows.medium

    # The ATR needs `window` bars, and the count then needs `window` bars
    # of measurable band on top of that.
    assert counts[QUIET].iloc[: 2 * window - 2].isna().all()
    assert counts[QUIET].iloc[-1] == counts[QUIET].iloc[-1]  # not NaN at the end
