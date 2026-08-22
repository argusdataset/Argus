"""Point-in-time timestamps, and how they are derived.

This is the leakage boundary. Every canonical record carries four
timestamps, and the rule that governs all of them is:

    A timestamp must reflect when ARGUS could actually have known the
    value — not when the underlying event occurred.

The concrete failure this prevents: a company's Q1 ends March 31 but is
not filed until May 15. Using the fiscal period end as `observation_time`
would let every backtest see Q1 numbers six weeks before they existed.
Nothing downstream would ever notice, and every feature derived from that
field would be quietly wrong for the life of the system. So fundamentals
take `observation_time` from the filing's accepted date, never from the
period end — see `data.normalization.translate`.

**When availability is uncertain, err late.** `availability_time` is not
directly reported by FMP, so it is derived as `observation_time` plus a
provider lag. Choosing a lag that is too long makes ARGUS mildly
pessimistic: a signal becomes usable slightly later than it really could
have been. Choosing one that is too short creates leakage, which is
unrecoverable and invisible. The asymmetry is the whole reason the
defaults below are generous rather than tight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

#: US equity markets. Bar timestamps are anchored to this, not to UTC
#: midnight, so a daily bar's event_time is the moment the session
#: actually closed.
MARKET_TIMEZONE = ZoneInfo("America/New_York")

#: Regular-session close for US equities.
MARKET_CLOSE = time(16, 0)


def session_close(bar_date: date) -> datetime:
    """The UTC instant a US equity session closed on `bar_date`.

    Anchored to 16:00 America/New_York and converted, so the result
    follows daylight saving rather than drifting an hour twice a year.
    Half-days (the early closes around some holidays) are not modelled:
    the difference is three hours on a handful of dates, and always in
    the conservative direction — ARGUS would consider the bar knowable
    slightly later than it truly was.
    """
    return datetime.combine(bar_date, MARKET_CLOSE, tzinfo=MARKET_TIMEZONE).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ProviderLagPolicy:
    """How long after something is knowable before ARGUS could fetch it.

    FMP does not report when a record entered its dataset, so
    `availability_time` has to be derived. These defaults are deliberately
    conservative — see the module docstring on why erring late is safe and
    erring early is not. They are a dataclass rather than constants so a
    caller can tighten them once real observations justify it.
    """

    #: Daily bars. FMP publishes EOD data within hours of the close, but
    #: consolidated values settle overnight; assuming next-morning
    #: availability avoids claiming same-session knowledge of a close.
    daily_bar: timedelta = timedelta(hours=16)

    #: Fundamentals, measured from the filing's accepted timestamp. SEC
    #: acceptance is public immediately, but a vendor's parse and
    #: publication lag behind it.
    fundamentals: timedelta = timedelta(hours=24)

    #: Corporate actions, measured from announcement (or effective date
    #: when no announcement date is supplied).
    corporate_action: timedelta = timedelta(hours=24)

    #: News. Aggregators surface articles quickly.
    news: timedelta = timedelta(minutes=30)


DEFAULT_LAG_POLICY = ProviderLagPolicy()


@dataclass(frozen=True, slots=True)
class PitTimestamps:
    """The four point-in-time timestamps every canonical record carries.

    Mirrors the non-nullable columns Module 03 put on every canonical
    table. Constructed only via `derive`, which enforces the ordering
    invariants, so a record cannot be built with an incoherent set.
    """

    event_time: datetime
    observation_time: datetime
    availability_time: datetime
    ingestion_time: datetime

    @classmethod
    def derive(
        cls,
        *,
        event_time: datetime,
        observation_time: datetime,
        ingestion_time: datetime,
        lag: timedelta,
        availability_time: datetime | None = None,
    ) -> PitTimestamps:
        """Build a coherent set, deriving availability when not supplied.

        `availability_time` defaults to `observation_time + lag`. It is
        additionally floored at `observation_time`: a provider cannot make
        available something that has not been observed yet.

        `ingestion_time` is NOT clamped. ARGUS legitimately fetches
        historical data years after it became available, so
        `ingestion_time` far exceeding `availability_time` is the normal
        case for a backfill, not an error.
        """
        if availability_time is None:
            availability_time = observation_time + lag
        availability_time = max(availability_time, observation_time)
        return cls(
            event_time=_as_utc(event_time),
            observation_time=_as_utc(observation_time),
            availability_time=_as_utc(availability_time),
            ingestion_time=_as_utc(ingestion_time),
        )

    def as_columns(self) -> dict[str, datetime]:
        """Mapping onto Module 03's canonical PIT column names."""
        return {
            "event_time": self.event_time,
            "observation_time": self.observation_time,
            "availability_time": self.availability_time,
            "ingestion_time": self.ingestion_time,
        }


def _as_utc(value: datetime) -> datetime:
    """Normalise to timezone-aware UTC.

    A naive datetime is treated as UTC rather than as local time: the
    process timezone is an accident of deployment and must never change
    what a historical timestamp means.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
