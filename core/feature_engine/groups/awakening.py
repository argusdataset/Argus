"""Group C — Awakening.

The base starts to stir: volatility and volume re-expand, price presses
the top of its range, relative strength turns up.

One deliberate design point, called out in the module brief: **relative
strength improvement frequently leads price**. So `rs_improvement_*` is
computed purely from the relative-strength line's own slope, with no
dependence on a price breakout having happened. That is what lets a
downstream module observe RS turning up *before* the breakout rather than
only confirming one after the fact — a feature that required the breakout
first would make the leading behaviour unobservable by construction.
"""

from __future__ import annotations

import pandas as pd

from core.feature_engine.groups.decline import _relative_strength_line
from core.feature_engine.panel import PricePanel, log_returns, true_range
from core.feature_engine.spec import FeatureSpec
from core.feature_engine.windows import rolling_slope, safe_ratio


def compute(
    panel: PricePanel,
    spec: FeatureSpec,
    *,
    market_close: pd.Series | None = None,
    sector_close: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Every Group C feature, as wide (dates × securities) frames."""
    windows, tolerances = spec.windows, spec.tolerances
    close, high, low, volume = panel.close_adj, panel.high_adj, panel.low_adj, panel.volume

    features: dict[str, pd.DataFrame] = {}

    # Volatility waking up — the inverse of Group B's compression.
    returns = log_returns(panel)
    recent_volatility = returns.rolling(windows.short).std()
    base_volatility = returns.rolling(windows.long).std()
    features["volatility_reexpansion"] = safe_ratio(recent_volatility, base_volatility)

    # Volume waking up.
    recent_volume = volume.rolling(windows.short).mean()
    base_volume = volume.rolling(windows.long).mean()
    features["volume_expansion"] = safe_ratio(recent_volume, base_volume)

    rvol = safe_ratio(volume, volume.rolling(windows.medium).mean())
    features["rvol_increase"] = rvol - rvol.shift(windows.short)

    # Bars getting wider.
    average_true_range = true_range(panel)
    features["range_expansion"] = safe_ratio(
        average_true_range.rolling(windows.short).mean(),
        average_true_range.rolling(windows.long).mean(),
    )

    # How hard price is pressing the top of its range: 0 at the low,
    # 1 at the high. Continuous, so "approaching resistance" is a
    # magnitude rather than a boolean.
    range_high = high.rolling(windows.medium).max()
    range_low = low.rolling(windows.medium).min()
    features["resistance_pressure"] = safe_ratio(close - range_low, range_high - range_low)

    # New local extremes becoming more frequent.
    is_higher_high = high > high.rolling(windows.pivot).max().shift(1)
    is_higher_low = low > low.rolling(windows.pivot).min().shift(1)
    features["higher_high_frequency"] = is_higher_high.astype(float).rolling(windows.medium).mean()
    features["higher_low_frequency"] = is_higher_low.astype(float).rolling(windows.medium).mean()

    # Momentum turning up — the mirror of Group A's deterioration.
    momentum = close.pct_change(windows.medium, fill_method=None)
    features["momentum_improvement"] = momentum - momentum.shift(windows.medium)

    # RS turning up. Computed from the RS line alone — see module docstring.
    features["rs_improvement_vs_market"] = _rs_slope(close, market_close, windows.medium)
    features["rs_improvement_vs_sector"] = _rs_slope(close, sector_close, windows.medium)

    # How much of the recent window price spent in the top of its range.
    upper_threshold = range_high - (range_high - range_low) * tolerances.upper_range
    in_upper = (close >= upper_threshold).astype(float)
    features["time_in_upper_range"] = in_upper.rolling(windows.medium).mean()

    return features


def _rs_slope(
    close: pd.DataFrame,
    benchmark: pd.Series | pd.DataFrame | None,
    window: int,
) -> pd.DataFrame:
    """Slope of the relative-strength line, normalized by its own level.

    Positive means the security is gaining on its benchmark. Independent
    of price action, so it can be observed rising before a breakout.
    NaN when no benchmark is available — never a zero that would read as
    "no improvement".
    """
    if benchmark is None:
        return pd.DataFrame(float("nan"), index=close.index, columns=close.columns)

    relative = _relative_strength_line(close, benchmark)
    return safe_ratio(rolling_slope(relative, window), relative)
