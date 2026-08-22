"""Pending material events against a real database, including the leak.

The load-bearing test in this file is
`test_a_historical_query_cannot_see_an_earnings_date_announced_later`.
Everything else here is supporting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.data_validation.result import MissReason
from core.risk_context.config import RiskThresholds
from core.risk_context.events import (
    EARNINGS,
    EARNINGS_SOURCE,
    LITIGATION,
    RESERVED_EVENT_TYPES,
    SOURCED_EVENT_TYPES,
    EventCoverage,
    pending_events_as_of,
    translate_earnings_event,
    write_material_events,
)
from data.provider_adapters.fmp.models import EarningsEvent, FetchProvenance

MARCH = datetime(2024, 3, 1, tzinfo=UTC)
APRIL = datetime(2024, 4, 1, tzinfo=UTC)
MAY = datetime(2024, 5, 1, tzinfo=UTC)


def _provenance(fetched_at: datetime) -> FetchProvenance:
    return FetchProvenance(
        endpoint="earnings_calendar", url_path="/earnings-calendar", fetched_at=fetched_at
    )


# --------------------------------------------------------------------------
# The leakage test
# --------------------------------------------------------------------------


def test_a_historical_query_cannot_see_an_earnings_date_announced_later(
    connection, register, add_event
):
    """The test this module exists to pass.

    An earnings release on 15 April, which ARGUS only learned about on
    1 April. A query as of 1 March must not see it — the date existed in
    the future either way, but ARGUS did not know it, and a backtest that
    flags event risk it could not have known about is reporting
    foreknowledge as skill.

    The failure this catches is a single-character one: filtering on
    `scheduled_for` (which is in the future in both queries) instead of on
    `availability_time` (which is not).
    """
    security_id = register("LEAK")
    add_event(
        security_id,
        scheduled_for=datetime(2024, 4, 15, tzinfo=UTC),
        available=APRIL,
    )

    before = pending_events_as_of(connection, security_id, as_of=MARCH)
    after = pending_events_as_of(connection, security_id, as_of=APRIL)

    # Before the announcement: nothing knowable, and honestly labelled.
    assert before.coverage is EventCoverage.UNAVAILABLE
    assert before.miss_reason is MissReason.NOT_YET_AVAILABLE
    assert before.events == ()
    assert before.days_until_next is None

    # After: the same row, now visible.
    assert after.coverage is EventCoverage.KNOWN
    assert after.next_event is not None
    assert after.next_event.scheduled_for == datetime(2024, 4, 15, tzinfo=UTC)


def test_the_leak_is_visible_when_the_availability_filter_is_bypassed(
    connection, register, add_event
):
    """Proof the test above is load-bearing rather than passing by luck.

    Runs the query the buggy implementation would run — filter on
    `scheduled_for` only — against the same row, and asserts it *does*
    leak. If the fixture were wrong (the row not written, the dates
    inverted), this would fail too, and the test above would be passing
    on an empty table.
    """
    from sqlalchemy import select

    from infra.db.schema.intelligence import pending_material_events

    security_id = register("LEAK2")
    add_event(
        security_id,
        scheduled_for=datetime(2024, 4, 15, tzinfo=UTC),
        available=APRIL,
    )

    leaky = connection.execute(
        select(pending_material_events).where(
            pending_material_events.c.security_id == security_id,
            pending_material_events.c.scheduled_for >= MARCH,
        )
    ).all()

    assert len(leaky) == 1, "fixture must contain exactly the row the PIT query hides"
    assert pending_events_as_of(connection, security_id, as_of=MARCH).events == ()


# --------------------------------------------------------------------------
# Coverage: no event vs. no data
# --------------------------------------------------------------------------


def test_never_ingested_and_not_yet_available_are_different_answers(
    connection, register, add_event
):
    """Both mean "no events returned"; only one means "nothing is scheduled"."""
    silent = register("SILENT")
    later = register("LATER")
    add_event(later, scheduled_for=datetime(2024, 4, 15, tzinfo=UTC), available=APRIL)

    never = pending_events_as_of(connection, silent, as_of=MARCH)
    not_yet = pending_events_as_of(connection, later, as_of=MARCH)

    assert never.coverage is EventCoverage.UNAVAILABLE
    assert never.miss_reason is MissReason.NEVER_INGESTED
    assert not_yet.coverage is EventCoverage.UNAVAILABLE
    assert not_yet.miss_reason is MissReason.NOT_YET_AVAILABLE


def test_covered_but_empty_is_a_real_negative(connection, register, add_event):
    """Calendar data was knowable, and nothing falls in the horizon.

    The only case where "no event" is information rather than ignorance.
    """
    security_id = register("QUIET")
    # A past earnings date: knowable, and behind us.
    add_event(
        security_id,
        scheduled_for=datetime(2024, 1, 15, tzinfo=UTC),
        available=datetime(2024, 1, 10, tzinfo=UTC),
    )

    view = pending_events_as_of(connection, security_id, as_of=MAY)

    assert view.coverage is EventCoverage.NONE_SCHEDULED
    assert view.miss_reason is None
    assert view.coverage_observed_at == datetime(2024, 1, 10, tzinfo=UTC)


def test_events_beyond_the_horizon_are_not_reported_as_upcoming(connection, register, add_event):
    """Past the horizon an earnings date is a near-certainty, not information —
    every company has one eventually."""
    security_id = register("FAR")
    thresholds = RiskThresholds()
    far = MARCH + timedelta(days=thresholds.event_horizon_days.value * 2)
    add_event(security_id, scheduled_for=far, available=datetime(2024, 2, 1, tzinfo=UTC))

    view = pending_events_as_of(connection, security_id, as_of=MARCH, thresholds=thresholds)

    assert view.coverage is EventCoverage.NONE_SCHEDULED


def test_the_soonest_event_comes_first(connection, register, add_event):
    security_id = register("MANY")
    for day in (60, 10, 30):
        add_event(
            security_id,
            scheduled_for=MARCH + timedelta(days=day),
            available=datetime(2024, 2, 1, tzinfo=UTC),
            event_type=EARNINGS if day == 10 else LITIGATION,
        )

    view = pending_events_as_of(connection, security_id, as_of=MARCH)

    assert len(view.events) == 3
    assert view.days_until_next == pytest.approx(10.0)
    assert view.next_event.event_type == EARNINGS


# --------------------------------------------------------------------------
# Re-observation
# --------------------------------------------------------------------------


def test_re_fetching_the_same_date_does_not_postpone_when_it_was_known(
    connection, register, add_event
):
    """A calendar fetched weekly writes the same event over and over.

    Collapsing those on the *latest* availability — the restatement rule —
    would report the event as first knowable at the most recent re-fetch,
    hiding knowledge ARGUS demonstrably already had and making an
    imminent event look newly discovered.
    """
    security_id = register("REPEAT")
    scheduled = datetime(2024, 4, 15, tzinfo=UTC)
    for observed in (MARCH, MARCH + timedelta(days=7), MARCH + timedelta(days=14)):
        add_event(security_id, scheduled_for=scheduled, available=observed, observed=observed)

    view = pending_events_as_of(connection, security_id, as_of=APRIL)

    assert len(view.events) == 1
    assert view.next_event.known_from == MARCH


def test_writing_the_same_observation_twice_inserts_nothing(connection, register):
    """Re-running ingestion is idempotent, matching Modules 05, 08 and 09."""
    security_id = register("IDEM")
    event = EarningsEvent(
        provenance=_provenance(MARCH),
        symbol="IDEM",
        earnings_date=datetime(2024, 4, 15, tzinfo=UTC).date(),
        eps_estimated=Decimal("1.25"),
    )
    record = translate_earnings_event(event, security_id)

    assert write_material_events(connection, [record]) == 1
    assert write_material_events(connection, [record]) == 0


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------


def test_translation_treats_the_fetch_time_as_the_moment_of_knowledge(connection, register):
    """FMP does not report when a schedule was announced. ARGUS's own
    observation is the only announcement it can evidence."""
    security_id = register("TRANS")
    thresholds = RiskThresholds()
    record = translate_earnings_event(
        EarningsEvent(
            provenance=_provenance(MARCH),
            symbol="TRANS",
            earnings_date=datetime(2024, 4, 15, tzinfo=UTC).date(),
        ),
        security_id,
        thresholds=thresholds,
    )

    assert record.event_type == EARNINGS
    assert record.is_binary is True
    assert record.source == EARNINGS_SOURCE
    assert record.pit.observation_time == MARCH
    assert record.pit.event_time == MARCH
    assert record.pit.availability_time == MARCH + thresholds.ingestion_lag_delta
    assert record.details["announcement_time_known"] is False


def test_a_backfilled_calendar_contributes_nothing_to_an_earlier_replay(connection, register):
    """The stated consequence of that choice, asserted rather than left in prose.

    An earnings date for 2020 fetched in 2024 is invisible to a 2020
    query. That is the honest answer — ARGUS did not know it — and it is
    why event-proximity risk is meaningful only from the point ARGUS
    started fetching calendars forward.
    """
    security_id = register("BACKFILL")
    record = translate_earnings_event(
        EarningsEvent(
            provenance=_provenance(datetime(2024, 1, 1, tzinfo=UTC)),
            symbol="BACKFILL",
            earnings_date=datetime(2020, 5, 15, tzinfo=UTC).date(),
        ),
        security_id,
    )
    write_material_events(connection, [record])

    replay = pending_events_as_of(connection, security_id, as_of=datetime(2020, 5, 1, tzinfo=UTC))

    assert replay.coverage is EventCoverage.UNAVAILABLE
    assert replay.miss_reason is MissReason.NOT_YET_AVAILABLE


# --------------------------------------------------------------------------
# Extensibility
# --------------------------------------------------------------------------


def test_an_unsourced_event_type_stores_and_queries_with_no_schema_change(
    connection, register, add_event
):
    """Module 03 made `event_type` free text so litigation, M&A, trial
    results and patent decisions need no migration. This asserts that is
    true in practice, not just in the column definition — a future module
    with a litigation source writes a new constant and nothing else.
    """
    security_id = register("LIT")
    add_event(
        security_id,
        scheduled_for=MARCH + timedelta(days=20),
        available=datetime(2024, 2, 1, tzinfo=UTC),
        event_type=LITIGATION,
        is_binary=True,
    )

    view = pending_events_as_of(connection, security_id, as_of=MARCH)

    assert view.coverage is EventCoverage.KNOWN
    assert view.next_event.event_type == LITIGATION
    assert LITIGATION in RESERVED_EVENT_TYPES
    assert LITIGATION not in SOURCED_EVENT_TYPES


def test_a_caller_can_restrict_the_query_to_one_event_type(connection, register, add_event):
    security_id = register("FILTER")
    add_event(
        security_id,
        scheduled_for=MARCH + timedelta(days=5),
        available=datetime(2024, 2, 1, tzinfo=UTC),
        event_type=LITIGATION,
    )
    add_event(
        security_id,
        scheduled_for=MARCH + timedelta(days=20),
        available=datetime(2024, 2, 1, tzinfo=UTC),
        event_type=EARNINGS,
    )

    earnings_only = pending_events_as_of(
        connection, security_id, as_of=MARCH, event_types=[EARNINGS]
    )

    assert [event.event_type for event in earnings_only.events] == [EARNINGS]
