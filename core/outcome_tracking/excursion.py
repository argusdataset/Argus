"""Measuring what actually happened, from prices ARGUS could have seen.

## Entry is the `activated` event, never the opening

A setup is opened when Module 10 first calls the base; it is *activated*
when the structure advances far enough for ARGUS to be tracking it in
earnest. Measuring from the opening would credit or blame the setup for
weeks of base-building that no position was ever taken through, and would
make MFE and MAE incomparable between a setup activated on day 3 and one
activated on day 300. A setup that never activated has **no entry**, and
this module reports no excursion for it rather than inventing one.

## The window ends where the setup ended

`[activation, min(terminal event, activation + horizon)]`. Measuring past
the terminal event would attribute price action to a setup ARGUS had
already stopped tracking — the very thing that makes a backtest flatter
than reality. Measuring past the horizon would let a criterion stated as
"within 60 trading days" quietly become "eventually".

The horizon is counted in **trading days** through Module 07's market
calendar, not in calendar days, so a holiday stretch does not silently
shorten it.

## Adjusted prices, PIT-bounded, both of which matter here

Prices come through Module 08's `load_panel`, which loads bars whose
`availability_time <= as_of` and applies only corporate actions knowable
by the same instant. Both halves are load-bearing and both fail silently
if got wrong:

* An unadjusted series turns a 2-for-1 split into a −50% single-bar
  excursion, so a successful setup is recorded as a catastrophic failure.
* A split filed *after* the computation date, applied anyway, rescales the
  whole window and changes MFE by the split ratio — a leak that produces
  a plausible number rather than an error.

`tests/integration/outcome_tracking/test_pit_leakage.py` constructs both
and proves this module's `as_of` reaches the loader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from sqlalchemy.engine import Connection

from core.data_validation.calendar import expected_trading_days
from core.feature_engine.panel import PricePanel, load_panel, true_range
from core.feature_engine.spec import FeatureSpec
from core.feature_engine.timeframes import periods_per_year
from core.outcome_tracking.config import OutcomeThresholds
from data.canonical_model.records import CanonicalTimeframe

#: Named reasons an excursion could not be measured. Reported, never
#: replaced by a zero.
NO_ENTRY_EVENT = "no_entry_event"
NO_BARS = "no_bars_in_window"
TOO_FEW_BARS = "too_few_bars"
NO_ENTRY_PRICE = "no_entry_price"
NO_BENCHMARK = "no_benchmark"
#: ATR could not be measured at entry — too little price history before
#: activation. The criterion cannot resolve without it: `target_hit_at`
#: and `stop_hit_at` both stay `None` rather than falling back to a flat
#: percentage, which would silently reintroduce the flaw this exists to
#: fix for exactly the securities it is riskiest to get wrong (those with
#: the shortest histories).
NO_ATR = "no_atr_at_entry"


@dataclass(frozen=True, slots=True)
class OutcomeWindow:
    """The span an outcome was measured over, and why it ends there."""

    entry_at: datetime
    ends_at: datetime
    #: Where the window's end came from: the terminal event or the
    #: horizon. Stored because "the setup ended" and "the clock ran out"
    #: are different facts about the same row.
    ends_because: str
    horizon_ends_at: datetime
    terminal_at: datetime

    @property
    def duration(self) -> timedelta:
        return self.ends_at - self.entry_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_at": self.entry_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "ends_because": self.ends_because,
            "horizon_ends_at": self.horizon_ends_at.isoformat(),
            "terminal_at": self.terminal_at.isoformat(),
            "duration_days": self.duration / timedelta(days=1),
        }


TERMINAL_EVENT = "terminal_event"
HORIZON = "horizon"


@dataclass(frozen=True, slots=True)
class Excursion:
    """Everything measured from price over one outcome window.

    Every field is `None` when it could not be measured, never zero —
    the discipline Module 08 established for features and Module 12 for
    risk flags. `unavailable` names why.
    """

    window: OutcomeWindow | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    mfe: float | None = None
    mae: float | None = None
    time_to_mfe: timedelta | None = None
    time_to_mae: timedelta | None = None
    realized_return: float | None = None
    benchmark_relative_return: float | None = None
    volatility_adjusted_outcome: float | None = None
    #: When the criterion's target or stop was first touched, if either
    #: was. Both None means the criterion did not resolve in the window.
    target_hit_at: datetime | None = None
    stop_hit_at: datetime | None = None
    #: The 20-session ATR at entry, in price units — not a fraction. None
    #: whenever it could not be measured (`NO_ATR`), in which case
    #: `target_threshold`/`stop_threshold` are None too and the criterion
    #: did not resolve.
    atr_at_entry: float | None = None
    #: The actual crossing levels this specific setup was measured
    #: against, as fractions of entry price — `target_atr_multiple *
    #: (atr_at_entry / entry_price)` and the stop's equivalent. Recorded
    #: rather than left implicit: the multiplier alone does not say what
    #: percentage move it meant for *this* security, and a reviewer
    #: reading one row should not have to recompute it.
    target_threshold: float | None = None
    stop_threshold: float | None = None
    bars_observed: int = 0
    unavailable: tuple[str, ...] = ()

    @property
    def measured(self) -> bool:
        return self.mfe is not None and self.mae is not None

    @property
    def criterion_resolved(self) -> bool:
        return self.target_hit_at is not None or self.stop_hit_at is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window.as_dict() if self.window else None,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "mfe": self.mfe,
            "mae": self.mae,
            "time_to_mfe_days": _days(self.time_to_mfe),
            "time_to_mae_days": _days(self.time_to_mae),
            "realized_return": self.realized_return,
            "benchmark_relative_return": self.benchmark_relative_return,
            "volatility_adjusted_outcome": self.volatility_adjusted_outcome,
            "target_hit_at": _iso(self.target_hit_at),
            "stop_hit_at": _iso(self.stop_hit_at),
            "atr_at_entry": self.atr_at_entry,
            "target_threshold": self.target_threshold,
            "stop_threshold": self.stop_threshold,
            "bars_observed": self.bars_observed,
            "unavailable": list(self.unavailable),
        }


def outcome_window(
    *,
    entry_at: datetime,
    terminal_at: datetime,
    thresholds: OutcomeThresholds,
) -> OutcomeWindow:
    """The span to measure over, and which of the two bounds closed it."""
    horizon_ends = horizon_end(entry_at, thresholds)
    ends_at = min(terminal_at, horizon_ends)
    return OutcomeWindow(
        entry_at=entry_at,
        ends_at=ends_at,
        ends_because=TERMINAL_EVENT if terminal_at <= horizon_ends else HORIZON,
        horizon_ends_at=horizon_ends,
        terminal_at=terminal_at,
    )


def horizon_end(entry_at: datetime, thresholds: OutcomeThresholds) -> datetime:
    """`entry_at` plus the criterion's horizon, counted in trading days.

    Walks Module 07's market calendar rather than multiplying by a
    calendar-day fudge factor: "within 60 trading days" is a statement
    about how many sessions the market had, and a stretch containing
    Thanksgiving and Christmas has fewer of them per calendar week.
    """
    wanted = int(thresholds.horizon_trading_days.value)
    span = timedelta(days=wanted)
    # Widen until the calendar actually yields enough sessions, rather
    # than guessing a calendar-to-session ratio. A holiday-heavy stretch
    # needs a wider span than an ordinary one, and the loop finds out
    # instead of assuming.
    while True:
        sessions = [
            day
            for day in expected_trading_days(entry_at.date(), (entry_at + span).date())
            if day > entry_at.date()
        ]
        if len(sessions) >= wanted:
            return _end_of_session(sessions[wanted - 1], entry_at)
        span *= 2


def measure(
    connection: Connection,
    security_id: UUID,
    *,
    window: OutcomeWindow,
    as_of: datetime,
    thresholds: OutcomeThresholds,
    benchmark_security_id: UUID | None = None,
) -> Excursion:
    """Excursions and returns over `window`, from bars knowable at `as_of`.

    `as_of` is a plain argument and is the only thing standing between
    this computation and a leak: it bounds both the bars and the corporate
    actions applied to them.
    """
    securities = [security_id]
    if benchmark_security_id is not None and benchmark_security_id != security_id:
        securities.append(benchmark_security_id)

    panel = load_panel(
        connection,
        securities,
        as_of,
        max_lookback_bars=_bars_to_load(window.entry_at, as_of, thresholds),
    )
    if panel.close_adj.empty or security_id not in panel.close_adj.columns:
        return Excursion(window=window, unavailable=(NO_BARS,))

    frame = _window_frame(panel, security_id, window)
    if len(frame) < int(thresholds.min_bars_for_outcome.value):
        return Excursion(
            window=window,
            bars_observed=len(frame),
            unavailable=(NO_BARS if frame.empty else TOO_FEW_BARS,),
        )

    entry_price = float(frame["close"].iloc[0])
    if not np.isfinite(entry_price) or entry_price <= 0.0:
        return Excursion(window=window, bars_observed=len(frame), unavailable=(NO_ENTRY_PRICE,))

    # Excursions from the bars *after* entry: the entry bar's own high and
    # low happened around the moment of entry, and counting them would let
    # a setup show favourable excursion it was never positioned for.
    after = frame.iloc[1:]
    high_ratio = after["high"] / entry_price - 1.0
    low_ratio = after["low"] / entry_price - 1.0

    mfe = float(high_ratio.max())
    mae = float(low_ratio.min())
    exit_price = float(frame["close"].iloc[-1])

    unavailable: list[str] = []
    benchmark = _benchmark_return(panel, benchmark_security_id, window, thresholds)
    if benchmark is None:
        unavailable.append(NO_BENCHMARK)

    # The criterion is volatility-normalized: a flat percentage applied
    # uniformly misclassifies outcomes across securities of different
    # volatility. ATR is measured at entry, from the same panel already
    # loaded, using Module 08's own primitive — see the module docstring.
    atr_window = int(FeatureSpec().windows.medium)
    atr = _atr_at_entry(panel, security_id, window.entry_at, atr_window)
    target_threshold: float | None = None
    stop_threshold: float | None = None
    target_hit_at: datetime | None = None
    stop_hit_at: datetime | None = None
    if atr is None:
        unavailable.append(NO_ATR)
    else:
        atr_fraction = atr / entry_price
        target_threshold = thresholds.target_atr_multiple.value * atr_fraction
        stop_threshold = thresholds.stop_atr_multiple.value * atr_fraction
        target_hit_at = _first_crossing(high_ratio, target_threshold, above=True)
        stop_hit_at = _first_crossing(low_ratio, stop_threshold, above=False)

    realized = exit_price / entry_price - 1.0
    return Excursion(
        window=window,
        entry_price=entry_price,
        exit_price=exit_price,
        mfe=mfe,
        mae=mae,
        time_to_mfe=_time_to(after, high_ratio.idxmax(), window),
        time_to_mae=_time_to(after, low_ratio.idxmin(), window),
        realized_return=realized,
        benchmark_relative_return=None if benchmark is None else realized - benchmark,
        volatility_adjusted_outcome=_volatility_adjusted(frame["close"], realized),
        target_hit_at=target_hit_at,
        stop_hit_at=stop_hit_at,
        atr_at_entry=atr,
        target_threshold=target_threshold,
        stop_threshold=stop_threshold,
        bars_observed=len(frame),
        unavailable=tuple(unavailable),
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _bars_to_load(entry_at: datetime, as_of: datetime, thresholds: OutcomeThresholds) -> int:
    """Enough lookback for the whole window, the entry bar, and the ATR.

    `max_lookback_bars` counts backward from `as_of`, not from
    `entry_at` — so on top of the session count spanning the outcome
    window itself, this adds the trailing ATR window (plus one, for
    `true_range`'s own `shift(1)` against the previous close) so the
    loaded panel actually reaches back before entry far enough to measure
    volatility *at* entry, not merely during the window that follows it.
    """
    sessions = expected_trading_days(entry_at.date(), as_of.date())
    atr_window = int(FeatureSpec().windows.medium)
    return max(len(sessions) + atr_window + 1, int(thresholds.min_bars_for_outcome.value))


def _window_frame(panel: PricePanel, security_id: UUID, window: OutcomeWindow) -> pd.DataFrame:
    """Adjusted high/low/close for the window, entry bar first.

    The entry bar is the last session at or before the activation instant
    — a setup activated intraday is entered at that session's close, not
    at the next one, which would skip a day of the window.
    """
    frame = pd.DataFrame(
        {
            "high": panel.high_adj[security_id],
            "low": panel.low_adj[security_id],
            "close": panel.close_adj[security_id],
        }
    ).dropna()
    if frame.empty:
        return frame

    at_or_before = frame.index[frame.index <= window.entry_at]
    if len(at_or_before) == 0:
        return frame.iloc[:0]
    start = at_or_before[-1]
    return frame.loc[(frame.index >= start) & (frame.index <= window.ends_at)]


def _atr_at_entry(
    panel: PricePanel, security_id: UUID, entry_at: datetime, window: int
) -> float | None:
    """The trailing `window`-session average true range, ending at entry.

    Module 08's own `true_range` (`core.feature_engine.panel`), applied to
    the same panel `measure()` already loaded and averaged over the same
    window Module 08 uses for its own ATR feature
    (`FeatureSpec().windows.medium`) — not a second implementation, and
    not read from a stored feature vector: a vector might not exist at
    the exact activation instant, and `select_latest_as_of` falling back
    to an older one could be stale by exactly the days that matter most
    for a volatility-normalized criterion. Recomputing from the panel
    already in hand keeps this measurement exactly as PIT-bounded as
    every other one in this module.

    `None` when there is not `window` sessions of history at or before
    entry — a security too newly listed to measure. Reported as `NO_ATR`
    rather than falling back to a flat percentage.
    """
    if security_id not in panel.close_adj.columns:
        return None
    ranges = true_range(panel)[security_id].dropna()
    at_or_before = ranges.loc[ranges.index <= entry_at]
    if len(at_or_before) < window:
        return None
    value = float(at_or_before.iloc[-window:].mean())
    return value if np.isfinite(value) and value > 0.0 else None


def _first_crossing(ratios: pd.Series, level: float, *, above: bool) -> datetime | None:
    """When the series first reached `level`, or None if it never did."""
    crossed = ratios >= level if above else ratios <= level
    hits = ratios.index[crossed]
    return None if len(hits) == 0 else hits[0].to_pydatetime()


def _time_to(frame: pd.DataFrame, stamp: Any, window: OutcomeWindow) -> timedelta | None:
    if frame.empty or stamp is None or pd.isna(stamp):
        return None
    return stamp.to_pydatetime() - window.entry_at


def _benchmark_return(
    panel: PricePanel,
    benchmark_security_id: UUID | None,
    window: OutcomeWindow,
    thresholds: OutcomeThresholds,
) -> float | None:
    """The benchmark's return over the same window, or None.

    None rather than zero: "the market was flat" and "we have no market
    series" are different facts, and a relative return computed against
    the second is a fabrication.
    """
    if benchmark_security_id is None or benchmark_security_id not in panel.close_adj.columns:
        return None
    series = panel.close_adj[benchmark_security_id].dropna()
    inside = series.loc[(series.index >= window.entry_at) & (series.index <= window.ends_at)]
    if len(inside) < int(thresholds.min_bars_for_outcome.value):
        return None
    first = float(inside.iloc[0])
    if not np.isfinite(first) or first <= 0.0:
        return None
    return float(inside.iloc[-1]) / first - 1.0


def _volatility_adjusted(closes: pd.Series, realized: float) -> float | None:
    """Realized return over the window's own annualized volatility.

    A 20% gain from a name that moves 5% a week and one that moves 40% a
    week are not the same result, and a dataset that records only the
    return teaches Module 17 that they are.
    """
    returns = np.log(closes / closes.shift(1)).dropna()
    if returns.empty:
        return None
    deviation = float(returns.std())
    if not np.isfinite(deviation) or deviation <= 0.0:
        return None
    annualized = deviation * float(np.sqrt(periods_per_year(CanonicalTimeframe.DAILY)))
    return realized / annualized


def _end_of_session(day: date, reference: datetime) -> datetime:
    """A calendar day as an instant, keeping the reference's time of day.

    Bars are stamped at the session close, so the horizon boundary has to
    land at or after that instant or the final session falls outside its
    own window.
    """
    return datetime.combine(day, reference.timetz())


def _days(value: timedelta | None) -> float | None:
    return None if value is None else value / timedelta(days=1)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
