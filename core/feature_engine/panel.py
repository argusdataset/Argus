"""The price panel: PIT-correct, adjusted, and shaped for vectorization.

A `PricePanel` holds one wide DataFrame per OHLCV field — index dates,
one column per security — so every feature calculation downstream is a
single pandas or numpy call covering the entire universe.

## The leakage vector this module has to get right

Module 05's `apply_adjustments` applies **every** corporate action ARGUS
has ever ingested. Using it to build a price series for a historical
`as_of` would let a 2015 feature reflect a split that was not knowable
until 2020 — a silent, plausible-looking wrong number, which is the
failure mode the whole architecture exists to prevent. Module 07 caught
this before it could be built in.

So this module never calls `apply_adjustments`. It builds adjustment
factors from `load_corporate_actions_as_of` — actions filtered to
`availability_time <= as_of` — and applies them itself, vectorized. Same
back-adjustment convention as Module 05 (most recent bar keeps its raw
price, earlier bars are scaled), different and strictly narrower input
set.

**Raw is retained alongside adjusted**, for the same reason Module 05
retains both: raw is the point-in-time reality a trader would have seen,
adjusted is what makes multi-year structure comparable. Features that
describe structure use adjusted; features that describe tradability
(dollar volume) use raw.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from sqlalchemy.engine import Connection

from core.data_validation.bulk import load_corporate_actions_as_of, load_ohlcv_panel_as_of
from data.canonical_model.records import CanonicalCorporateActionType, CanonicalTimeframe

# One implementation of "what ratio does this split describe", shared with
# Module 05's adjustment path. Two implementations is what let this one
# quietly lack both the field tolerance and the reporting the other had.
from data.normalization.translate import SPLIT_FIELD_ALIASES, split_ratio

#: Standard-library logging rather than `infra.observability.logging`.
#: That package's `__init__` reaches `core.model_validation_evaluation`,
#: which imports this module's own package — so importing it here is a
#: cycle. The records still flow through the configured handlers; only
#: the convenience wrapper is skipped, the same choice
#: `services/identity/seam.py` made for its own reasons.
_log = logging.getLogger("argus.feature_engine.panel")


def _resolve_ratio(details: object) -> float | None:
    """`split_ratio` as a float, or None when the payload is unusable.

    A thin adapter rather than a second implementation: Module 05 owns
    what a split ratio *is* and this file needs it as a float for the
    pandas arithmetic below. Returning None rather than defaulting to 1.0
    keeps an unreadable split visible as an unadjusted series instead of
    silently producing a discontinuity that looks like a real 75% crash.
    """
    if not isinstance(details, dict):
        return None
    ratio = split_ratio(details)
    return None if ratio is None else float(ratio)


#: Calendar days of slack per bar of lookback, so a window of N bars is
#: satisfied despite weekends and holidays. Generous on purpose — loading
#: slightly too much history is harmless; loading too little silently
#: truncates a window and produces a NaN where a number was expected.
CALENDAR_DAYS_PER_BAR = 1.75


@dataclass(slots=True)
class PricePanel:
    """Wide OHLCV frames for a set of securities, all sharing an index."""

    open_adj: pd.DataFrame
    high_adj: pd.DataFrame
    low_adj: pd.DataFrame
    close_adj: pd.DataFrame
    close_raw: pd.DataFrame
    volume: pd.DataFrame
    #: Latest `availability_time` contributing to each security's series.
    #: Module 03 defines feature_vectors.availability_time as the max
    #: across inputs; this is where that max comes from.
    availability: pd.Series = field(default_factory=pd.Series)

    @property
    def securities(self) -> list[UUID]:
        return list(self.close_adj.columns)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close_adj.index

    def bar_counts(self) -> pd.Series:
        """Non-null bars per security — the evidence basis for sufficiency."""
        return self.close_adj.notna().sum(axis=0)

    def is_empty(self) -> bool:
        return self.close_adj.empty or self.close_adj.shape[1] == 0


def required_history_start(
    as_of: datetime, max_lookback_bars: int, timeframe: CanonicalTimeframe
) -> datetime:
    """How far back to load so `max_lookback_bars` are actually available.

    Bounds the load: a feature engine needs its longest window, not the
    full ~15 years of the universe, and loading the latter to compute one
    date would be gratuitous.
    """
    bars_in_daily_terms = max_lookback_bars * _daily_bars_per_period(timeframe)
    days = int(bars_in_daily_terms * CALENDAR_DAYS_PER_BAR) + 10
    return as_of - timedelta(days=days)


def _daily_bars_per_period(timeframe: CanonicalTimeframe) -> int:
    return {
        CanonicalTimeframe.DAILY: 1,
        CanonicalTimeframe.WEEKLY: 5,
        CanonicalTimeframe.MONTHLY: 21,
    }.get(timeframe, 1)


def load_panel(
    connection: Connection,
    security_ids: list[UUID],
    as_of: datetime,
    *,
    max_lookback_bars: int,
    apply_corporate_actions: bool = True,
) -> PricePanel:
    """Load a PIT-correct, adjusted daily panel for `security_ids`.

    One query for the bars, one for the corporate actions, regardless of
    how many securities are requested. `as_of` is a plain argument — a
    live call and a historical-replay call are the same call with a
    different value, exactly as Module 07's `get_as_of` established.
    """
    start = required_history_start(as_of, max_lookback_bars, CanonicalTimeframe.DAILY)
    bars = load_ohlcv_panel_as_of(connection, security_ids, as_of, start=start)

    if bars.empty:
        empty = pd.DataFrame()
        return PricePanel(
            empty, empty, empty, empty, empty, empty, pd.Series(dtype="datetime64[ns, UTC]")
        )

    def wide(column: str) -> pd.DataFrame:
        return bars.pivot(index="event_time", columns="security_id", values=column).sort_index()

    open_raw, high_raw, low_raw = wide("open_raw"), wide("high_raw"), wide("low_raw")
    close_raw, volume = wide("close_raw"), wide("volume_raw")
    availability = bars.groupby("security_id")["availability_time"].max()

    if apply_corporate_actions:
        actions = load_corporate_actions_as_of(connection, security_ids, as_of)
        factors = build_adjustment_factors(actions, close_raw)
    else:
        factors = pd.DataFrame(1.0, index=close_raw.index, columns=close_raw.columns)

    return PricePanel(
        open_adj=open_raw * factors,
        high_adj=high_raw * factors,
        low_adj=low_raw * factors,
        close_adj=close_raw * factors,
        close_raw=close_raw,
        volume=volume,
        availability=availability,
    )


def build_adjustment_factors(actions: pd.DataFrame, close_raw: pd.DataFrame) -> pd.DataFrame:
    """Cumulative back-adjustment factor per (date, security).

    Only the actions passed in are applied — and the caller is required to
    have obtained them from `load_corporate_actions_as_of`, i.e. filtered
    to `availability_time <= as_of`. That restriction is the entire point:
    an action knowable only later must not touch a historical price
    series.

    Convention matches Module 05: bars strictly *before* an action's
    effective date are scaled; the bar on the effective date already
    reflects it. The most recent bar keeps its raw price, so "today's
    price" stays a real number.

    A split whose ratio cannot be read is **left unadjusted and logged**,
    never skipped in silence. `data/normalization/adjustments.py` has
    always reported the same case through `report.skip`; this side did
    not, so one rule had two implementations that disagreed about
    whether anyone should be told. The count is what makes a provider
    field rename visible on its first day rather than as a puzzling
    −50% bar months later.

    Splits only. Dividend adjustment is deliberately omitted here — see
    the module README; the structural features this panel feeds are
    price-shape measurements, and a dividend-adjusted series would shift
    every historical level by an amount unrelated to the structure being
    measured.
    """
    factors = pd.DataFrame(1.0, index=close_raw.index, columns=close_raw.columns)
    if actions.empty:
        return factors

    splits = actions[actions["action_type"] == CanonicalCorporateActionType.SPLIT.value]
    if splits.empty:
        return factors

    unresolved: list[tuple[Any, Any]] = []
    for row in splits.itertuples():
        security_id = row.security_id
        if security_id not in factors.columns:
            continue
        ratio = _resolve_ratio(row.details)
        if ratio is None:
            # Reported, never silent. An unresolved split is not a gap in
            # a panel — it is a price series that is wrong from the split
            # date backwards, and Module 15 records the resulting −50%
            # bar as a catastrophic failure of a setup that succeeded.
            # That is issue G2's own failure mode returning without a
            # single error message, which is how it went unnoticed for as
            # long as it did.
            unresolved.append((security_id, row.effective_date))
            continue
        effective = _effective_day(row.effective_date)
        # Vectorized over every date at once for this security.
        # Bars are session closes; `effective` is midnight of its day, so a
        # bar ON the effective day is correctly excluded. See `_effective_day`.
        earlier = factors.index < effective
        factors.loc[earlier, security_id] *= 1.0 / ratio

    if unresolved:
        _log.warning(
            "split ratios could not be resolved; those series are unadjusted",
            extra={
                "event": "split_ratio_unresolved",
                "unresolved_splits": len(unresolved),
                "securities": len({str(security) for security, _date in unresolved}),
                "effective_dates": sorted({str(date) for _security, date in unresolved}),
                "tried": list(SPLIT_FIELD_ALIASES["numerator"])
                + list(SPLIT_FIELD_ALIASES["denominator"])
                + list(SPLIT_FIELD_ALIASES["ratio"]),
            },
        )

    return factors


def _effective_day(value: object) -> pd.Timestamp:
    """An action's effective date reduced to midnight UTC.

    Bars are timestamped at the session close (20:00 UTC) while
    `canonical_corporate_actions.effective_date` is a `DateTime` column
    holding what Module 05 models as a plain `date`. Comparing the two
    directly worked only because that column happens to be written at
    midnight: an effective date stored at, say, 23:59 would make the
    session-close bar on the effective day compare as "earlier" and be
    adjusted a second time, carving a one-bar 75% hole into the middle of
    an otherwise continuous series.

    Module 05's `apply_adjustments` compares `factor.effective_date >
    bar_date` — date against date. This does the same, so correctness no
    longer depends on a coincidence between two layers' time-of-day
    conventions.
    """
    effective = pd.Timestamp(value)
    if effective.tzinfo is None:
        effective = effective.tz_localize("UTC")
    return effective.normalize()


def true_range(panel: PricePanel) -> pd.DataFrame:
    """Wilder's true range, vectorized across the universe.

    `np.maximum` reduces the three candidate ranges element-wise over the
    whole (dates × securities) block at once.
    """
    previous_close = panel.close_adj.shift(1)
    high_low = panel.high_adj - panel.low_adj
    high_close = (panel.high_adj - previous_close).abs()
    low_close = (panel.low_adj - previous_close).abs()
    return pd.DataFrame(
        np.maximum(np.maximum(high_low.to_numpy(), high_close.to_numpy()), low_close.to_numpy()),
        index=panel.close_adj.index,
        columns=panel.close_adj.columns,
    )


def log_returns(panel: PricePanel) -> pd.DataFrame:
    """Log returns of the adjusted close, with non-positive prices as NaN."""
    prices = panel.close_adj.where(panel.close_adj > 0)
    return np.log(prices / prices.shift(1))
