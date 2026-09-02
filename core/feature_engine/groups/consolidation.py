"""Group B — Consolidation.

The base: price stops falling and coils. These features describe how
tight, how quiet, and how well-defended that range is.

The rule that matters most here is the one about scales versus gates. A
three-week base and a three-year base must both be measurable, so
`normalized_range_width` measures a range over a fixed *ruler* (the
medium window) and reports a number — it never asks "has this lasted long
enough to count as a base". `atr_percentile` is likewise relative to the
security's own trailing distribution, never an absolute cutoff that would
mean different things for a $2 stock and a $2,000 one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.feature_engine.panel import PricePanel, log_returns, true_range
from core.feature_engine.spec import FeatureSpec
from core.feature_engine.timeframes import periods_per_year
from core.feature_engine.windows import rolling_percentile_rank, rolling_slope, safe_ratio
from data.canonical_model.records import CanonicalTimeframe


def compute(
    panel: PricePanel,
    spec: FeatureSpec,
    *,
    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY,
) -> dict[str, pd.DataFrame]:
    """Every Group B feature, as wide (dates × securities) frames."""
    windows, tolerances = spec.windows, spec.tolerances
    close, high, low, volume = panel.close_adj, panel.high_adj, panel.low_adj, panel.volume

    range_high = high.rolling(windows.medium).max()
    range_low = low.rolling(windows.medium).min()

    features: dict[str, pd.DataFrame] = {}

    # How wide the range is as a fraction of price — scale-free, so a
    # $2 stock and a $2,000 stock are directly comparable.
    features["normalized_range_width"] = safe_ratio(range_high - range_low, close)

    average_true_range = true_range(panel).rolling(windows.medium).mean()
    features["atr"] = average_true_range

    # Where current ATR sits in the security's OWN trailing distribution.
    features["atr_percentile"] = rolling_percentile_rank(average_true_range, windows.percentile)

    returns = log_returns(panel)
    annualisation = np.sqrt(periods_per_year(timeframe))
    features["realized_volatility"] = returns.rolling(windows.medium).std() * annualisation

    # Compression: recent volatility against the preceding stretch.
    # Below 1 means coiling.
    recent_volatility = returns.rolling(windows.medium).std()
    features["volatility_compression"] = safe_ratio(
        recent_volatility, recent_volatility.shift(windows.medium)
    )

    average_volume = volume.rolling(windows.medium).mean()
    features["volume_contraction"] = safe_ratio(
        average_volume, average_volume.shift(windows.medium)
    )
    features["rvol"] = safe_ratio(volume, average_volume)

    # How often price returned to the edges of its range. A well-tested
    # level is a more meaningful level.
    #
    # The band is a multiple of this security's own ATR, not a fixed
    # fraction of price: a flat band counts volatility as much as it
    # counts level-testing, because price sits inside a fixed percentage
    # window in inverse proportion to how far it travels per session. See
    # `FeatureTolerances` in `spec.py`.
    # `.where(measurable)` is load-bearing, not defensive. ATR is a
    # rolling mean, so it is NaN until the window fills — and `NaN <= NaN`
    # is `False`, not NaN. Without the mask those early bars would enter
    # the count as measured non-tests, which is a fabricated zero standing
    # in for an absent measurement: exactly what Module 08 refuses to do
    # everywhere else.
    level_band = average_true_range * tolerances.level_test_atr
    measurable = level_band.notna()
    near_support = ((low - range_low).abs() <= level_band).where(measurable)
    near_resistance = ((high - range_high).abs() <= level_band).where(measurable)
    features["support_test_count"] = near_support.astype(float).rolling(windows.medium).sum()
    features["resistance_test_count"] = near_resistance.astype(float).rolling(windows.medium).sum()

    # Probes that failed to follow through — evidence the range is
    # holding rather than breaking.
    prior_low = range_low.shift(1)
    prior_high = range_high.shift(1)
    failed_breakdown = (low < prior_low) & (close > prior_low)
    failed_breakout = (high > prior_high) & (close < prior_high)
    features["failed_breakdown_count"] = (
        failed_breakdown.astype(float).rolling(windows.medium).sum()
    )
    features["failed_breakout_count"] = failed_breakout.astype(float).rolling(windows.medium).sum()

    # Are the lows rising? Slope of the rolling low, normalized by price
    # so it is comparable across securities.
    features["higher_low_development"] = safe_ratio(
        rolling_slope(low.rolling(windows.pivot).min(), windows.medium), close
    )

    # Structure as a measured trend, NOT a bearish/neutral/bullish flag.
    # Price slope scaled by ATR: how decisively price is moving relative
    # to its own noise. Negative = still bearish, rising through zero =
    # transitioning.
    features["structure_transition"] = safe_ratio(
        rolling_slope(close, windows.medium), average_true_range
    )

    return features
