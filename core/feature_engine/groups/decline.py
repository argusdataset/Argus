"""Group A — Prior Decline & Stabilization.

The first phase of the ARGUS setup: a security that fell hard, and is
beginning to stop falling. Every feature is a continuous measurement over
the whole universe at once; none of them classifies anything, and none
branches on how long the decline lasted — `decline_duration_bars` is
*reported* as a feature precisely so that later modules can weigh it
rather than this one gating on it.
"""

from __future__ import annotations

import pandas as pd

from core.feature_engine.panel import PricePanel, log_returns
from core.feature_engine.spec import FeatureSpec
from core.feature_engine.windows import (
    bars_since_window_max,
    safe_ratio,
    window_min_after_max,
)


def compute(
    panel: PricePanel,
    spec: FeatureSpec,
    *,
    market_close: pd.Series | None = None,
    sector_close: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Every Group A feature, as wide (dates × securities) frames."""
    windows = spec.windows
    close = panel.close_adj

    structural_peak = close.rolling(windows.structural).max()
    trough_after_peak = window_min_after_max(close, windows.structural)

    features: dict[str, pd.DataFrame] = {}

    # How deep the fall from the structural peak actually went. Negative.
    features["peak_to_trough_decline"] = safe_ratio(
        trough_after_peak - structural_peak, structural_peak
    )

    # Bars since that peak — a measurement, never a gate.
    features["decline_duration_bars"] = bars_since_window_max(close, windows.structural)

    # Decline per bar: magnitude spread over the time it took. A fast
    # 60% crash and a slow 60% grind are structurally different setups,
    # and this is what distinguishes them.
    features["decline_speed"] = safe_ratio(
        features["peak_to_trough_decline"], features["decline_duration_bars"]
    )

    # Where price sits now relative to the structural peak.
    features["drawdown_pct"] = safe_ratio(close - structural_peak, structural_peak)

    # Momentum worsening: recent rate of change minus the prior period's.
    momentum = close.pct_change(windows.medium, fill_method=None)
    features["momentum_deterioration"] = momentum - momentum.shift(windows.medium)

    # Relative strength decaying against market and sector, over the
    # structural window — the cumulative relative damage a prior decline
    # did, which is the question Group A exists to answer.
    features["rs_deterioration_vs_market"] = _relative_damage(
        close, market_close, windows.structural
    )
    features["rs_deterioration_vs_sector"] = _relative_damage(
        close, sector_close, windows.structural
    )

    # Distance below the highest price ever seen in the loaded history.
    # Expanding, not rolling: "historical high" means all history ARGUS
    # can see as of this date, which the PIT-bounded panel already is.
    all_time_high = close.expanding().max()
    features["distance_from_historical_high"] = safe_ratio(close - all_time_high, all_time_high)

    # How often the security is still making new lows. Falling toward
    # zero is the signature of a decline losing force.
    is_lower_low = close < close.rolling(windows.pivot).min().shift(1)
    lower_low_frequency = is_lower_low.astype(float).rolling(windows.medium).mean()
    features["lower_low_frequency"] = lower_low_frequency

    # The improvement itself: positive when new lows are becoming rarer.
    features["downside_momentum_reduction"] = (
        lower_low_frequency.shift(windows.medium) - lower_low_frequency
    )

    # Volatility starting to contract — the first hint of stabilization.
    returns = log_returns(panel)
    recent_volatility = returns.rolling(windows.medium).std()
    prior_volatility = recent_volatility.shift(windows.medium)
    features["volatility_contraction_onset"] = safe_ratio(recent_volatility, prior_volatility)

    return features


def _relative_damage(
    close: pd.DataFrame,
    benchmark: pd.Series | pd.DataFrame | None,
    window: int,
) -> pd.DataFrame:
    """Fractional change in the relative-strength line over the window.

    Negative means the security lost ground to its benchmark. Measured as
    a *fractional* change rather than as a slope, because the RS line's
    level moves by a factor of two or more across a real decline and an
    absolute slope is not comparable to itself once it has.

    `tests/unit/feature_engine/test_feature_groups.py` caught the
    alternative the hard way. This was originally the second difference of
    `rolling_slope(relative, medium)` — "is the RS line's slope getting
    worse". At the bottom of a synthetic 55% relative-strength collapse it
    read **+0.0018**, i.e. mildly improving, because the RS line had
    halved: the same fractional decay produces a smaller absolute slope on
    a smaller level, and "less negative slope" is indistinguishable from
    recovery. Exactly the plausible-looking wrong number the project is
    built to avoid, and invisible without a direction test.

    Returns an all-NaN frame when no benchmark is available — a missing
    sector benchmark produces an honest NaN, never a zero that would read
    as "no deterioration".
    """
    if benchmark is None:
        return pd.DataFrame(float("nan"), index=close.index, columns=close.columns)

    relative = _relative_strength_line(close, benchmark)
    return relative.pct_change(window, fill_method=None)


def _relative_strength_line(
    close: pd.DataFrame, benchmark: pd.Series | pd.DataFrame
) -> pd.DataFrame:
    """Security price divided by its benchmark, aligned on dates.

    A `Series` benchmark (the market) is broadcast across every security;
    a `DataFrame` benchmark (per-security sector proxies) is aligned
    column-wise.
    """
    if isinstance(benchmark, pd.Series):
        aligned = benchmark.reindex(close.index).replace(0.0, float("nan"))
        return close.div(aligned, axis=0)

    aligned = benchmark.reindex(index=close.index, columns=close.columns)
    return close / aligned.replace(0.0, float("nan"))
