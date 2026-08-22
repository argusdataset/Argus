"""Group E — Context. Computed for every security on every date.

Unlike Groups A-D, which describe where a security is in the setup
lifecycle, these describe its surroundings — and are therefore computed
unconditionally rather than at particular pattern stages.

## Two boundary notes, both flagged rather than guessed

**Market regime.** This module emits regime-relevant *measurements* —
the benchmark's trend, its volatility, its drawdown — and deliberately
stops there. It does not label a regime "risk-on" or "bear". Module 03's
schema puts market state in `market_state` / `market_state_transitions`,
owned by Module 10's Market State Engine, with its own enum and
transition history; producing a competing regime label here would create
a second source of truth for the same question. See the module README.

**Sector and industry.** `rs_vs_sector` and `rs_vs_industry` require a
security-to-sector mapping, and **ARGUS does not currently store one** —
`security_identity` has no sector or industry column, and no canonical
table holds FMP's company-profile data. These features therefore accept
an optional caller-supplied benchmark and return NaN (with a recorded
`MissReason`) when none is available. Fabricating a sector from, say, a
ticker prefix would be worse than admitting the gap. Flagged for Module
09 in the report.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.feature_engine.panel import PricePanel
from core.feature_engine.spec import FeatureSpec
from core.feature_engine.timeframes import periods_per_year
from core.feature_engine.windows import rolling_slope, safe_ratio
from data.canonical_model.records import CanonicalTimeframe


def compute(
    panel: PricePanel,
    spec: FeatureSpec,
    *,
    market_close: pd.Series | None = None,
    sector_close: pd.DataFrame | None = None,
    industry_close: pd.DataFrame | None = None,
    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY,
) -> dict[str, pd.DataFrame]:
    """Every Group E feature, as wide (dates × securities) frames."""
    windows = spec.windows
    close, volume = panel.close_adj, panel.volume

    features: dict[str, pd.DataFrame] = {}

    # Relative return over the medium window against each benchmark.
    security_return = close.pct_change(windows.medium, fill_method=None)
    features["rs_vs_market"] = _relative_return(security_return, market_close, windows.medium)
    features["rs_vs_sector"] = _relative_return(security_return, sector_close, windows.medium)
    features["rs_vs_industry"] = _relative_return(security_return, industry_close, windows.medium)

    # Regime-relevant INPUTS only — not a regime label. See module docstring.
    features["market_regime_trend"] = _broadcast(_market_trend(market_close, windows.long), close)
    features["market_regime_volatility"] = _broadcast(
        _market_volatility(market_close, windows.medium, timeframe), close
    )
    features["market_regime_drawdown"] = _broadcast(
        _market_drawdown(market_close, windows.structural), close
    )

    # Liquidity. Uses RAW close, not adjusted: dollar volume is a
    # statement about tradability on the day, and a split-adjusted price
    # multiplied by unadjusted volume would understate it by the split
    # factor for every bar before the split.
    features["avg_dollar_volume"] = (panel.close_raw * volume).rolling(windows.medium).mean()

    # Spread proxy from OHLC. ARGUS has no quote data, so a true bid-ask
    # spread is not derivable; the normalized high-low range is the
    # standard stand-in, and is labelled a proxy rather than a spread.
    features["spread_proxy"] = safe_ratio(
        (panel.high_adj - panel.low_adj).rolling(windows.medium).mean(), close
    )

    return features


def _relative_return(
    security_return: pd.DataFrame,
    benchmark: pd.Series | pd.DataFrame | None,
    window: int,
) -> pd.DataFrame:
    """Security return minus benchmark return over the same window."""
    if benchmark is None:
        return pd.DataFrame(
            float("nan"), index=security_return.index, columns=security_return.columns
        )

    if isinstance(benchmark, pd.Series):
        benchmark_return = benchmark.reindex(security_return.index).pct_change(
            window, fill_method=None
        )
        return security_return.sub(benchmark_return, axis=0)

    aligned = benchmark.reindex(index=security_return.index, columns=security_return.columns)
    return security_return - aligned.pct_change(window, fill_method=None)


def _market_trend(market_close: pd.Series | None, window: int) -> pd.Series | None:
    """Benchmark slope, normalized by level. Positive = uptrend."""
    if market_close is None:
        return None
    frame = market_close.to_frame("m")
    slope = rolling_slope(frame, window)["m"]
    return slope / market_close.replace(0.0, np.nan)


def _market_volatility(
    market_close: pd.Series | None, window: int, timeframe: CanonicalTimeframe
) -> pd.Series | None:
    """Benchmark realized volatility, annualized."""
    if market_close is None:
        return None
    prices = market_close.where(market_close > 0)
    returns = np.log(prices / prices.shift(1))
    return returns.rolling(window).std() * np.sqrt(periods_per_year(timeframe))


def _market_drawdown(market_close: pd.Series | None, window: int) -> pd.Series | None:
    """Benchmark distance below its own trailing peak. Negative."""
    if market_close is None:
        return None
    peak = market_close.rolling(window).max()
    return (market_close - peak) / peak.replace(0.0, np.nan)


def _broadcast(series: pd.Series | None, template: pd.DataFrame) -> pd.DataFrame:
    """Repeat a market-wide series across every security's column.

    Regime is a property of the market, not of one security, but feature
    vectors are per-security — so every security on a given date carries
    the same regime reading.
    """
    if series is None:
        return pd.DataFrame(float("nan"), index=template.index, columns=template.columns)
    aligned = series.reindex(template.index)
    return pd.DataFrame(
        np.repeat(aligned.to_numpy()[:, None], template.shape[1], axis=1),
        index=template.index,
        columns=template.columns,
    )
