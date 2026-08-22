"""Deriving weekly and monthly panels from daily — and why H4 cannot be.

`infra/db/README.md` records the decision that H4/Weekly/Monthly bars are
derived from daily rather than fetched. This module owns that derivation.

## Weekly and monthly: deterministic

Standard OHLCV rollup — first open, max high, min low, last close, summed
volume — over calendar periods. Nothing is invented; every output value
appears in, or is a simple reduction of, the daily bars beneath it.

**Only completed periods are emitted.** A half-finished week has a
smaller range and lower volume than a full one purely because it is
half-finished, so mixing it into a rolling ATR would put a systematically
understated reading at the most recent (and most decision-relevant) bar.
Weekly features therefore lag by up to a week, which is honest rather
than convenient. Documented as a judgment call in the module README.

## H4: not derivable, and not faked

**No feature is computable at H4 resolution from daily source data.** A
daily bar is one OHLCV tuple per session; the intraday path that produced
it — where within the day the high occurred, whether the close was near
the high, how the four-hour blocks were shaped — is simply not present.
It has been discarded before the data ever reaches ARGUS.

Anything labelled "H4 derived from daily" would have to invent that
structure. The two obvious approaches are both fabrication:

- Forward-filling a daily bar into six H4 buckets yields six identical
  bars — a flat intraday path that never happened, producing a true range
  of zero for five of six bars and a volatility reading that is pure
  artifact.
- Interpolating between daily closes invents a smooth path, which would
  systematically *understate* intraday volatility and range expansion —
  and those are precisely the Group B/C features H4 would exist to
  measure.

So this module refuses H4 rather than returning a plausible-looking
number. Real H4 features require intraday source data (FMP's Ultimate
tier exposes 1-minute/intraday history); that is a data-acquisition
decision, not a derivation this module can perform. `derive_panel` raises
`UnsupportedTimeframeError` with that explanation.
"""

from __future__ import annotations

import pandas as pd

from core.feature_engine.panel import PricePanel
from data.canonical_model.records import CanonicalTimeframe

#: Pandas resample rules per derived timeframe. Weeks end Friday, matching
#: the US equity trading week.
_RESAMPLE_RULES: dict[CanonicalTimeframe, str] = {
    CanonicalTimeframe.WEEKLY: "W-FRI",
    CanonicalTimeframe.MONTHLY: "ME",
}

#: Timeframes this module can produce from a daily source panel.
DERIVABLE_TIMEFRAMES: tuple[CanonicalTimeframe, ...] = (
    CanonicalTimeframe.DAILY,
    CanonicalTimeframe.WEEKLY,
    CanonicalTimeframe.MONTHLY,
)


class UnsupportedTimeframeError(ValueError):
    """A timeframe that cannot be honestly derived from daily bars."""


def derive_panel(panel: PricePanel, timeframe: CanonicalTimeframe) -> PricePanel:
    """Aggregate a daily panel to `timeframe`.

    DAILY returns the panel unchanged. WEEKLY/MONTHLY roll up with
    standard OHLCV rules, keeping only completed periods. H4 raises.
    """
    if timeframe is CanonicalTimeframe.H4:
        raise UnsupportedTimeframeError(
            "H4 features cannot be derived from daily bars: a daily bar carries no "
            "intraday structure, so any H4 series would be fabricated (forward-fill "
            "invents flat bars, interpolation invents a smooth path and understates "
            "exactly the range/volatility features H4 would measure). Real H4 "
            "features require intraday source data. See core/feature_engine/README.md."
        )
    if timeframe is CanonicalTimeframe.DAILY:
        return panel
    if timeframe not in _RESAMPLE_RULES:
        raise UnsupportedTimeframeError(f"Unhandled timeframe: {timeframe!r}")

    rule = _RESAMPLE_RULES[timeframe]
    return PricePanel(
        open_adj=_resample(panel.open_adj, rule, "first"),
        high_adj=_resample(panel.high_adj, rule, "max"),
        low_adj=_resample(panel.low_adj, rule, "min"),
        close_adj=_resample(panel.close_adj, rule, "last"),
        close_raw=_resample(panel.close_raw, rule, "last"),
        volume=_resample(panel.volume, rule, "sum"),
        # Unchanged: a derived bar is available no earlier than the latest
        # daily bar that completes it, and this is already that maximum.
        availability=panel.availability,
    )


def _resample(frame: pd.DataFrame, rule: str, how: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    resampled = getattr(frame.resample(rule), how)()
    return _drop_incomplete_final_period(frame, resampled, rule)


def _drop_incomplete_final_period(
    daily: pd.DataFrame, resampled: pd.DataFrame, rule: str
) -> pd.DataFrame:
    """Remove a trailing period the daily data does not fully cover.

    The final resampled bucket is complete only if the daily panel extends
    to (or past) that bucket's own period end. Otherwise it is a partial
    period, and dropping it is what keeps the most recent bar from being
    systematically understated.
    """
    if resampled.empty or daily.empty:
        return resampled
    final_period_end = resampled.index[-1]
    if daily.index[-1] < final_period_end:
        return resampled.iloc[:-1]
    return resampled


def periods_per_year(timeframe: CanonicalTimeframe) -> int:
    """Annualisation factor for realized volatility at each timeframe."""
    return {
        CanonicalTimeframe.DAILY: 252,
        CanonicalTimeframe.WEEKLY: 52,
        CanonicalTimeframe.MONTHLY: 12,
    }.get(timeframe, 252)
