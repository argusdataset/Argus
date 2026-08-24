"""PIT-correct, split-adjusted OHLCV for one security over a range.

## Composed from Module 07 and Module 08, not rewritten

Three existing public functions do all the work:

- `load_ohlcv_panel_as_of` (Module 07) applies the restatement rule inside
  the database — `DISTINCT ON (security_id, event_time)` ordered by
  `availability_time DESC`, so each bar is the most recent revision that
  was actually knowable by `as_of`.
- `load_corporate_actions_as_of` (Module 07) returns only the actions
  knowable by then. This pairing is load-bearing: adjusting a 2015 series
  with a split announced in 2020 is the exact leak Module 08's panel
  builder exists to prevent.
- `build_adjustment_factors` (Module 08) turns those into per-bar factors,
  splits only.

A chart-specific query here would have to re-derive all three, and would
get the second one wrong first.

## Why the chart shows adjusted prices

An unadjusted series has a cliff at every split, and a user looking at a
base-and-breakout structure would see a pattern that never happened.
Module 08 adjusts for splits and deliberately not for dividends — a
dividend-adjusted series shifts every historical level by an amount
unrelated to the structure being measured — and a chart drawn on the same
basis as the features is a chart that agrees with the rest of ARGUS.

The most recent bar keeps its raw price, so "today's price" on the chart
is a real number.

## Weekly and monthly are derived, not stored

Only DAILY comes from a provider. Module 08's `derive_panel` rolls daily
up to weekly/monthly with standard OHLCV rules, keeping only completed
periods, so ARGUS keeps point-in-time control of the aggregation instead
of inheriting a vendor's. The datafeed serves whatever it derives; it does
not ask the provider for a weekly bar and it does not invent one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import pandas as pd
from sqlalchemy.engine import Connection

from core.data_validation.bulk import load_corporate_actions_as_of, load_ohlcv_panel_as_of
from core.feature_engine.panel import PricePanel, build_adjustment_factors
from core.feature_engine.timeframes import derive_panel
from data.canonical_model.records import CanonicalTimeframe

__all__ = ["BarSeries", "load_bars"]


@dataclass(frozen=True, slots=True)
class BarSeries:
    """One security's bars over a range, oldest first.

    Parallel lists rather than a list of objects, because that is the
    shape TradingView's protocol wants and building objects only to take
    them apart again would be work for nobody.
    """

    security_id: UUID
    timeframe: CanonicalTimeframe
    #: Unix seconds, UTC. The protocol's unit.
    times: list[int]
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    volumes: list[float]
    #: The oldest bar ARGUS holds for this security, when the requested
    #: range came back empty. Lets the datafeed answer `nextTime` instead
    #: of leaving the widget paging backwards forever.
    earliest_available: int | None = None

    def __len__(self) -> int:
        return len(self.times)

    @property
    def is_empty(self) -> bool:
        return not self.times


def load_bars(
    connection: Connection,
    security_id: UUID,
    *,
    start: datetime,
    end: datetime,
    as_of: datetime,
    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY,
    max_bars: int | None = None,
) -> BarSeries:
    """Adjusted bars in `[start, end]`, as knowable at `as_of`.

    `end` is applied after loading rather than in SQL. Module 07's loader
    bounds history at the front (`start`) and at `as_of`, which is what a
    feature engine needs; a chart asking for a window that ends in the
    past is the one case that wants a back bound too. Filtering in pandas
    for a single security is cheap, and the alternative — a second
    loader, or an argument added to Module 07's — would either duplicate
    the restatement logic or change a module this one is a consumer of.

    `max_bars` keeps the most recent bars when the range holds more,
    because a chart paging backwards wants the newest end of what it
    asked for.
    """
    # Weekly and monthly are derived from daily, so the underlying load is
    # always daily regardless of what was asked for.
    bars = load_ohlcv_panel_as_of(
        connection, [security_id], as_of, start=start, timeframe=CanonicalTimeframe.DAILY
    )
    if bars.empty:
        return _empty(security_id, timeframe)

    panel = _panel(connection, bars, [security_id], as_of)
    if timeframe is not CanonicalTimeframe.DAILY:
        panel = derive_panel(panel, timeframe)

    frame = _series_frame(panel, security_id)
    earliest = _unix(frame.index.min()) if not frame.empty else None

    windowed = frame[
        (frame.index >= _naive_aware(start, frame)) & (frame.index <= _naive_aware(end, frame))
    ]
    if max_bars is not None and len(windowed) > max_bars:
        windowed = windowed.tail(max_bars)

    if windowed.empty:
        return _empty(security_id, timeframe, earliest_available=earliest)

    return BarSeries(
        security_id=security_id,
        timeframe=timeframe,
        times=[_unix(stamp) for stamp in windowed.index],
        opens=[float(value) for value in windowed["open"]],
        highs=[float(value) for value in windowed["high"]],
        lows=[float(value) for value in windowed["low"]],
        closes=[float(value) for value in windowed["close"]],
        volumes=[float(value) for value in windowed["volume"]],
        earliest_available=earliest,
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _panel(
    connection: Connection, bars: pd.DataFrame, security_ids: list[UUID], as_of: datetime
) -> PricePanel:
    """The same assembly Module 08's `load_panel` performs, over a given range.

    Not a call to `load_panel` itself only because that function derives
    its start from a lookback-in-bars, and a chart has an explicit
    calendar range. Everything it does after that point is these three
    lines.
    """

    def wide(column: str) -> pd.DataFrame:
        return bars.pivot(index="event_time", columns="security_id", values=column).sort_index()

    open_raw, high_raw = wide("open_raw"), wide("high_raw")
    low_raw, close_raw = wide("low_raw"), wide("close_raw")
    volume = wide("volume_raw")

    actions = load_corporate_actions_as_of(connection, security_ids, as_of)
    factors = build_adjustment_factors(actions, close_raw)

    return PricePanel(
        open_adj=open_raw * factors,
        high_adj=high_raw * factors,
        low_adj=low_raw * factors,
        close_adj=close_raw * factors,
        close_raw=close_raw,
        volume=volume,
        availability=bars.groupby("security_id")["availability_time"].max(),
    )


def _series_frame(panel: PricePanel, security_id: UUID) -> pd.DataFrame:
    """One security's columns as a tidy frame, with incomplete bars dropped.

    A bar missing its close is a bar ARGUS cannot draw. Dropping it is the
    honest move; forward-filling would invent a flat bar that never
    traded.
    """
    if panel.close_adj.empty or security_id not in panel.close_adj.columns:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    frame = pd.DataFrame(
        {
            "open": panel.open_adj[security_id],
            "high": panel.high_adj[security_id],
            "low": panel.low_adj[security_id],
            "close": panel.close_adj[security_id],
            "volume": panel.volume[security_id],
        }
    )
    return frame.dropna(subset=["close"]).fillna({"volume": 0.0}).sort_index()


def _empty(
    security_id: UUID,
    timeframe: CanonicalTimeframe,
    *,
    earliest_available: int | None = None,
) -> BarSeries:
    return BarSeries(
        security_id=security_id,
        timeframe=timeframe,
        times=[],
        opens=[],
        highs=[],
        lows=[],
        closes=[],
        volumes=[],
        earliest_available=earliest_available,
    )


def _unix(stamp: pd.Timestamp) -> int:
    return int(pd.Timestamp(stamp).timestamp())


def _naive_aware(moment: datetime, frame: pd.DataFrame) -> pd.Timestamp:
    """Match the frame's tz-awareness so a comparison does not raise."""
    stamp = pd.Timestamp(moment)
    index_tz = getattr(frame.index, "tz", None)
    if index_tz is None:
        return stamp.tz_localize(None) if stamp.tzinfo else stamp
    return stamp.tz_convert(index_tz) if stamp.tzinfo else stamp.tz_localize(index_tz)
