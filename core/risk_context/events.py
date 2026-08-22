"""Pending material events: what is scheduled, and when ARGUS learned of it.

## Why the type column is free text, and what that buys

Module 03 made `pending_material_events.event_type` a `Text` column
rather than an enum, with the reason written into the schema: litigation,
M&A, trial results and patent decisions "must be addable without a schema
migration". This module honours that rather than re-tightening it — the
type names below are *reserved constants in Python*, not a database
domain. Adding `TRIAL_RESULT` when a source for it exists is a new
constant and an ingestion function; it is not a migration, not an enum
value, and not a change to anything that already reads the table.

Only `EARNINGS` is sourced today. The others are declared here so that
when a source appears the string is already agreed rather than invented
independently at three call sites, which is the failure mode a free-text
column otherwise invites.

## The point-in-time problem, stated honestly

FMP's earnings calendar reports *when an earnings release is scheduled*.
It does not report *when that schedule was announced*. Those are different
facts, and only the second one governs leakage.

Faced with that gap there are two options:

1. Assume the schedule was known some fixed time before the date. This
   invents foreknowledge — and would let a backtest in 2015 see a date
   that was in reality announced weeks later.
2. Treat the schedule as knowable only from the moment ARGUS actually
   fetched it.

This module takes option 2. `observation_time` is the fetch timestamp
(`FetchProvenance.fetched_at`), `event_time` is the same instant — the
"announcement" ARGUS can evidence is its own observation — and
`availability_time` is derived from it by `PitTimestamps.derive` with this
module's own ingestion lag.

**The consequence is real and worth stating plainly: an earnings calendar
backfilled today contributes nothing to a historical replay.** A query as
of 2015 sees no events, and correctly reports that as *data unavailable*
rather than as *no event scheduled*. Event-proximity risk becomes
meaningful only for dates at or after the point where ARGUS started
fetching calendars forward. That is a limitation of the data, and this
module's job is to make it visible rather than to paper over it.

## Re-observation is not restatement

The same earnings date is returned by every calendar fetch that spans it,
each with a later `observation_time` — Module 03's unique constraint
includes `observation_time` precisely so those land as separate rows. They
are not restatements of each other: a fact once known stays known, so the
moment that matters is the **earliest** availability among the rows for
one (event_type, scheduled_for), not the latest.

That is why this module does not call `select_latest_as_of`. That helper
implements the restatement rule — among knowable rows, take the newest —
which is right for a fundamentals figure that gets revised and wrong here:
it would report an event as first knowable at the most recent re-fetch,
hiding knowledge ARGUS demonstrably already had. The `availability_time <=
as_of` filter, which is the part that prevents leakage, is identical
either way.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.data_validation.result import MissReason
from core.risk_context.config import RiskThresholds
from data.canonical_model.pit import PitTimestamps
from data.provider_adapters.fmp.models import EarningsEvent
from infra.db.schema.intelligence import pending_material_events

# --------------------------------------------------------------------------
# Event types. Reserved names, not a closed domain.
# --------------------------------------------------------------------------

#: The only type with a data source at MVP.
EARNINGS = "EARNINGS"

#: Reserved for when a source exists. Declared, deliberately not sourced —
#: see the EXPLICIT BOUNDARIES of the Module 12 brief.
LITIGATION = "LITIGATION"
MERGER_ACQUISITION = "MERGER_ACQUISITION"
TRIAL_RESULT = "TRIAL_RESULT"
PATENT_DECISION = "PATENT_DECISION"

#: Names this module has agreed on. The database accepts any string; this
#: tuple exists so two callers do not independently coin "M&A" and
#: "MERGER_ACQUISITION" for the same thing.
RESERVED_EVENT_TYPES: tuple[str, ...] = (
    EARNINGS,
    LITIGATION,
    MERGER_ACQUISITION,
    TRIAL_RESULT,
    PATENT_DECISION,
)

#: Types ARGUS can actually populate today. Everything else in
#: `RESERVED_EVENT_TYPES` is a name waiting for a source.
SOURCED_EVENT_TYPES: frozenset[str] = frozenset({EARNINGS})

#: Where an earnings row came from, recorded in `source`.
EARNINGS_SOURCE = "fmp:earnings_calendar"


class EventCoverage(StrEnum):
    """Whether an event answer is information or an absence of information.

    The distinction the Module 12 brief asks for by name: a candidate with
    no upcoming earnings and a candidate whose calendar was never fetched
    look identical if both are reported as "no event", and only one of
    them is actually low-risk.
    """

    #: At least one event is scheduled within the horizon.
    KNOWN = "known"
    #: Calendar data for this security was knowable as of the query, and
    #: nothing falls within the horizon. A real negative.
    NONE_SCHEDULED = "none_scheduled"
    #: Nothing can be said. `miss_reason` distinguishes "never ingested"
    #: from "ingested, but not yet knowable at this as_of".
    UNAVAILABLE = "unavailable"


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MaterialEventRecord:
    """One event ready to be written to `pending_material_events`."""

    security_id: UUID
    event_type: str
    scheduled_for: datetime
    is_binary: bool
    pit: PitTimestamps
    details: dict[str, Any] = field(default_factory=dict)
    source: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "security_id": self.security_id,
            "event_type": self.event_type,
            "scheduled_for": self.scheduled_for,
            "is_binary": self.is_binary,
            "details": self.details,
            "source": self.source,
            **self.pit.as_columns(),
        }


@dataclass(frozen=True, slots=True)
class PendingEvent:
    """One scheduled event, as it was knowable at query time."""

    security_id: UUID
    event_type: str
    scheduled_for: datetime
    is_binary: bool
    #: Earliest `availability_time` across the rows recording this event.
    #: The first moment ARGUS could have acted on it — see the module
    #: docstring on why earliest, not latest.
    known_from: datetime
    details: dict[str, Any] = field(default_factory=dict)
    source: str | None = None

    def days_until(self, as_of: datetime) -> float:
        """Calendar days from `as_of` to the scheduled instant.

        Negative if the event has already passed; the caller decides
        whether that is interesting. Fractional, because "tomorrow
        morning" and "in six days" are different risks and rounding to
        whole days loses the first one.
        """
        return (self.scheduled_for - as_of) / timedelta(days=1)


@dataclass(frozen=True, slots=True)
class PendingEventsView:
    """What was scheduled for one security, as of one instant.

    Carries `coverage` alongside `events` so an empty tuple is never
    ambiguous.
    """

    security_id: UUID
    as_of: datetime
    horizon_days: float
    coverage: EventCoverage
    #: Events within the horizon, soonest first. Empty unless coverage is
    #: KNOWN.
    events: tuple[PendingEvent, ...] = ()
    #: Set iff coverage is UNAVAILABLE.
    miss_reason: MissReason | None = None
    #: Latest availability among the rows visible at `as_of` — how fresh
    #: the calendar coverage was. None when there was none.
    coverage_observed_at: datetime | None = None

    @property
    def next_event(self) -> PendingEvent | None:
        return self.events[0] if self.events else None

    @property
    def days_until_next(self) -> float | None:
        """Days to the soonest event, or None. Never 0.0 as a stand-in."""
        event = self.next_event
        return None if event is None else event.days_until(self.as_of)

    def as_dict(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage.value,
            "miss_reason": self.miss_reason.value if self.miss_reason else None,
            "horizon_days": self.horizon_days,
            "coverage_observed_at": (
                self.coverage_observed_at.isoformat() if self.coverage_observed_at else None
            ),
            "events": [
                {
                    "event_type": event.event_type,
                    "scheduled_for": event.scheduled_for.isoformat(),
                    "is_binary": event.is_binary,
                    "known_from": event.known_from.isoformat(),
                    "days_until": event.days_until(self.as_of),
                    "source": event.source,
                }
                for event in self.events
            ],
        }


# --------------------------------------------------------------------------
# Ingestion — Module 04's earnings calendar into Module 03's table
# --------------------------------------------------------------------------


def translate_earnings_event(
    event: EarningsEvent,
    security_id: UUID,
    *,
    thresholds: RiskThresholds | None = None,
) -> MaterialEventRecord:
    """One FMP earnings row as a canonical material-event record.

    `observation_time` is the fetch timestamp, not the earnings date and
    not a guessed announcement date. See the module docstring: the
    provider does not report when the schedule was announced, and
    inventing that timestamp is the one mistake here that would be
    invisible and unrecoverable.

    `is_binary` is True for earnings without qualification. That is the
    whole reason the schedule is tracked: a base that resolves on a
    scheduled binary outcome is a different proposition from one that
    resolves on structure, even when the two look identical on a chart.
    """
    thresholds = thresholds or RiskThresholds()
    observed = _as_utc(event.provenance.fetched_at)
    scheduled = _midnight_utc(event.earnings_date)

    return MaterialEventRecord(
        security_id=security_id,
        event_type=EARNINGS,
        scheduled_for=scheduled,
        is_binary=True,
        pit=PitTimestamps.derive(
            # ARGUS's own observation is the only announcement it can
            # evidence, so event_time and observation_time coincide.
            event_time=observed,
            observation_time=observed,
            ingestion_time=observed,
            lag=thresholds.ingestion_lag_delta,
        ),
        details={
            "symbol": event.symbol,
            "timing": event.timing,
            "eps_estimated": _decimal(event.eps_estimated),
            "revenue_estimated": _decimal(event.revenue_estimated),
            # Present only on rows the calendar has already resolved.
            # Retained because it makes a stored row self-describing, and
            # deliberately not used by anything in this module.
            "eps_actual": _decimal(event.eps_actual),
            "revenue_actual": _decimal(event.revenue_actual),
            "announcement_time_known": False,
            "observation_basis": "fetch_time",
        },
        source=EARNINGS_SOURCE,
    )


def write_material_events(connection: Connection, records: Iterable[MaterialEventRecord]) -> int:
    """Insert event records, skipping ones already recorded. Returns rows written.

    `ON CONFLICT DO NOTHING` against Module 03's
    `(security_id, event_type, scheduled_for, observation_time)`
    constraint, matching Modules 05, 08 and 09: a re-run inserts what is
    missing and never rewrites what is there. A re-fetch at a later time
    has a later `observation_time` and therefore lands as a new row, which
    is correct — it is a new observation of the same event, and the query
    layer collapses the group by earliest availability.
    """
    rows = [record.as_row() for record in records]
    if not rows:
        return 0

    statement = (
        insert(pending_material_events)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["security_id", "event_type", "scheduled_for", "observation_time"]
        )
        .returning(pending_material_events.c.id)
    )
    return len(connection.execute(statement).fetchall())


# --------------------------------------------------------------------------
# Point-in-time query
# --------------------------------------------------------------------------


def pending_events_as_of(
    connection: Connection,
    security_id: UUID,
    *,
    as_of: datetime,
    thresholds: RiskThresholds | None = None,
    event_types: Sequence[str] | None = None,
) -> PendingEventsView:
    """Events scheduled for `security_id` that were knowable at `as_of`.

    `as_of` is a plain argument, per `CROSS_CUTTING_REQUIREMENTS.md`: the
    same call answers for today or for 2015.

    Two filters, doing different jobs, and conflating them is the leak
    this function exists to prevent:

    * `availability_time <= as_of` decides what ARGUS **knew**.
    * `scheduled_for` within the horizon decides what is **upcoming**.

    Filtering on `scheduled_for` alone would let a date announced next
    month appear in a query about last month. The knowability filter is
    applied first and unconditionally.
    """
    thresholds = thresholds or RiskThresholds()
    horizon_days = thresholds.event_horizon_days.value
    horizon_end = as_of + timedelta(days=horizon_days)

    query = select(pending_material_events).where(
        pending_material_events.c.security_id == security_id
    )
    if event_types is not None:
        query = query.where(pending_material_events.c.event_type.in_(list(event_types)))
    rows = connection.execute(query).all()

    if not rows:
        # No row at any availability_time: the calendar was never
        # ingested for this security. Distinguishable from the case below
        # only because this query is deliberately unconstrained by as_of —
        # the "later, unconstrained lookup" MissReason.NEVER_INGESTED is
        # documented as requiring.
        return PendingEventsView(
            security_id=security_id,
            as_of=as_of,
            horizon_days=horizon_days,
            coverage=EventCoverage.UNAVAILABLE,
            miss_reason=MissReason.NEVER_INGESTED,
        )

    knowable = [row for row in rows if row.availability_time <= as_of]
    if not knowable:
        return PendingEventsView(
            security_id=security_id,
            as_of=as_of,
            horizon_days=horizon_days,
            coverage=EventCoverage.UNAVAILABLE,
            miss_reason=MissReason.NOT_YET_AVAILABLE,
        )

    observed_at = max(row.availability_time for row in knowable)
    upcoming = sorted(
        (
            event
            for event in _collapse_observations(knowable)
            if as_of <= event.scheduled_for <= horizon_end
        ),
        key=lambda event: event.scheduled_for,
    )

    return PendingEventsView(
        security_id=security_id,
        as_of=as_of,
        horizon_days=horizon_days,
        coverage=EventCoverage.KNOWN if upcoming else EventCoverage.NONE_SCHEDULED,
        events=tuple(upcoming),
        coverage_observed_at=observed_at,
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _collapse_observations(rows: Iterable[Any]) -> list[PendingEvent]:
    """One `PendingEvent` per (type, scheduled_for), keyed on first knowledge.

    Every calendar fetch spanning an event writes another row for it. They
    describe the same event, so they collapse — on the *earliest*
    availability, because knowledge is not un-learned.
    """
    grouped: dict[tuple[str, datetime], PendingEvent] = {}
    for row in rows:
        key = (row.event_type, row.scheduled_for)
        existing = grouped.get(key)
        if existing is not None and existing.known_from <= row.availability_time:
            continue
        grouped[key] = PendingEvent(
            security_id=row.security_id,
            event_type=row.event_type,
            scheduled_for=row.scheduled_for,
            is_binary=bool(row.is_binary),
            known_from=row.availability_time,
            details=dict(row.details or {}),
            source=row.source,
        )
    return list(grouped.values())


def _midnight_utc(value: Any) -> datetime:
    """A calendar date as the UTC instant it begins.

    Deliberately the start of the day rather than the market close: an
    earnings release scheduled for a date is treated as arriving as early
    as that date could deliver it, so proximity is never understated.
    """
    if isinstance(value, datetime):
        return _as_utc(value)
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal(value: Any) -> float | None:
    """Decimal to float for JSONB, preserving None as None."""
    return None if value is None else float(value)
