"""Group D — Confirmation.

Price actually clears the range and holds. Everything here is a
*magnitude*, never a boolean: `resistance_breakout_pct` is how far
through the level price is (negative when still below), not `has_broken
== True`. A downstream module can threshold a magnitude however it
likes; it cannot recover a magnitude from a boolean this module already
threw away.
"""

from __future__ import annotations

import pandas as pd

from core.feature_engine.groups.decline import _relative_strength_line
from core.feature_engine.panel import PricePanel
from core.feature_engine.spec import FeatureSpec
from core.feature_engine.windows import rolling_slope, safe_ratio


def compute(
    panel: PricePanel,
    spec: FeatureSpec,
    *,
    market_close: pd.Series | None = None,
    sector_close: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Every Group D feature, as wide (dates × securities) frames."""
    windows = spec.windows
    close, high, low, volume = panel.close_adj, panel.high_adj, panel.low_adj, panel.volume

    # The resistance level being tested: the prior window's high,
    # shifted so the current bar cannot define the level it is breaking.
    resistance = high.rolling(windows.medium).max().shift(1)

    features: dict[str, pd.DataFrame] = {}

    # Distance through the level, as a fraction. Negative below it.
    features["resistance_breakout_pct"] = safe_ratio(close - resistance, resistance)

    # Conviction behind the move.
    features["breakout_volume_ratio"] = safe_ratio(volume, volume.rolling(windows.medium).mean())

    # Did price stay above the level, or was it a one-bar spike? Fraction
    # of the recent window closing above resistance.
    features["acceptance_followthrough"] = (
        (close > resistance).astype(float).rolling(windows.short).mean()
    )

    # Retest quality: how far the recent low dipped relative to the
    # broken level. Positive means the level held as support.
    features["retest_and_hold"] = safe_ratio(
        low.rolling(windows.short).min() - resistance, resistance
    )

    # How decisively the new high exceeds the prior one.
    prior_high = high.rolling(windows.medium).max().shift(windows.medium)
    features["higher_high_magnitude"] = safe_ratio(
        high.rolling(windows.medium).max() - prior_high, prior_high
    )

    # Alignment: is the security rising *and* outperforming? The product
    # of the two slopes is positive only when they agree, and its
    # magnitude reflects how strongly.
    price_slope = safe_ratio(rolling_slope(close, windows.medium), close)
    features["rs_alignment_market"] = _alignment(close, market_close, price_slope, windows.medium)
    features["rs_alignment_sector"] = _alignment(close, sector_close, price_slope, windows.medium)

    return features


def _alignment(
    close: pd.DataFrame,
    benchmark: pd.Series | pd.DataFrame | None,
    price_slope: pd.DataFrame,
    window: int,
) -> pd.DataFrame:
    """Agreement between price trend and relative-strength trend.

    Positive when both are rising (or both falling); negative when they
    disagree. NaN without a benchmark rather than a misleading zero.
    """
    if benchmark is None:
        return pd.DataFrame(float("nan"), index=close.index, columns=close.columns)

    relative = _relative_strength_line(close, benchmark)
    rs_slope = safe_ratio(rolling_slope(relative, window), relative)
    return price_slope * rs_slope
