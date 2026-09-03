"""When is a security due a deep refresh? The four cases the tiers exist for.

Pure decision, so every case is a constructed call with no database and
no clock. The four tests named in the module's brief are here verbatim,
plus the cases that make them meaningful: the same-day re-run, the
security on no list, and the escalation's mirror image.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.ingestion.config import IngestionSettings
from core.ingestion.tiers import (
    ALREADY_REFRESHED,
    INTERVAL_ELAPSED,
    NEVER_REFRESHED,
    NO_TIER,
    NOT_DUE,
    TIER_ESCALATION,
    RefreshRecord,
    decide,
)

TODAY = date(2026, 3, 10)
SETTINGS = IngestionSettings()


def _decide(watchlist: str | None, last: RefreshRecord | None, *, on: date = TODAY):
    return decide(watchlist=watchlist, last=last, target_date=on, settings=SETTINGS)


def test_a_security_never_refreshed_is_due():
    decision = _decide("DOWN_TREND", None)

    assert decision.due
    assert decision.trigger == NEVER_REFRESHED


def test_a_down_trend_security_refreshed_today_is_not_due_again_for_thirty_days():
    """The first case in the brief, asserted across the whole interval.

    Written as a sweep rather than at day 29 and day 30 because the claim
    is about the interval, not about its endpoints: a rule that happened
    to be right at the boundary and wrong at day 12 would pass a
    two-point test.
    """
    last = RefreshRecord(watchlist="DOWN_TREND", refreshed_on=TODAY)
    interval = SETTINGS.interval_days("DOWN_TREND")

    for elapsed in range(1, interval):
        decision = _decide("DOWN_TREND", last, on=TODAY + timedelta(days=elapsed))
        assert not decision.due, elapsed
        assert decision.trigger == NOT_DUE

    on_time = _decide("DOWN_TREND", last, on=TODAY + timedelta(days=interval))
    assert on_time.due
    assert on_time.trigger == INTERVAL_ELAPSED


def test_a_promotion_to_consolidation_two_days_later_is_due_immediately():
    """The reason this module reads a last-known phase at all.

    Two days after a DOWN_TREND refresh there are twenty-eight days left
    on that interval. The security is no longer on it, and interval
    arithmetic against `refreshed_on` alone cannot see that — nothing
    about the elapsed time changed when the phase did.
    """
    last = RefreshRecord(watchlist="DOWN_TREND", refreshed_on=TODAY)

    decision = _decide("CONSOLIDATION", last, on=TODAY + timedelta(days=2))

    assert decision.due
    assert decision.trigger == TIER_ESCALATION
    assert "DOWN_TREND" in decision.reason and "CONSOLIDATION" in decision.reason


@pytest.mark.parametrize("watchlist", ["BREAKOUT_READY", "UPTREND"])
def test_the_urgent_lists_are_due_every_day(watchlist: str):
    last = RefreshRecord(watchlist=watchlist, refreshed_on=TODAY)

    decision = _decide(watchlist, last, on=TODAY + timedelta(days=1))

    assert decision.due
    assert decision.trigger == INTERVAL_ELAPSED


@pytest.mark.parametrize(
    "promotion",
    [
        ("DOWN_TREND", "CONSOLIDATION"),
        ("DOWN_TREND", "BREAKOUT_READY"),
        ("DOWN_TREND", "UPTREND"),
        ("CONSOLIDATION", "BREAKOUT_READY"),
        ("CONSOLIDATION", "UPTREND"),
    ],
)
def test_every_move_to_a_shorter_tier_escalates(promotion: tuple[str, str]):
    """Escalation is derived from the intervals, not a hand-written order.

    So this is the complete set of ordered pairs whose interval strictly
    decreases, and every one of them has to fire — an order maintained by
    hand would eventually miss one of these.
    """
    prior, current = promotion
    last = RefreshRecord(watchlist=prior, refreshed_on=TODAY)

    decision = _decide(current, last, on=TODAY + timedelta(days=1))

    assert decision.due
    assert decision.trigger == TIER_ESCALATION


def test_a_demotion_does_not_escalate_and_waits_out_the_new_interval():
    """The mirror image, which must *not* fire.

    A security falling out of UPTREND into DOWN_TREND is less urgent, not
    more. Refreshing it immediately would spend a request to learn that
    nothing needs watching.
    """
    last = RefreshRecord(watchlist="UPTREND", refreshed_on=TODAY)

    decision = _decide("DOWN_TREND", last, on=TODAY + timedelta(days=1))

    assert not decision.due
    assert decision.trigger == NOT_DUE


def test_breakout_ready_to_uptrend_is_not_an_escalation():
    """Both are daily, so there is nothing to escalate to.

    It is still due — the daily interval has elapsed — but by the
    interval rule. That distinction is what proves urgency is read from
    the intervals rather than from the order the lists are written in.
    """
    last = RefreshRecord(watchlist="BREAKOUT_READY", refreshed_on=TODAY)

    decision = _decide("UPTREND", last, on=TODAY + timedelta(days=1))

    assert decision.due
    assert decision.trigger == INTERVAL_ELAPSED


def test_a_second_run_on_the_same_day_refreshes_nothing():
    """Even when the phase escalated between the two runs.

    The log table's unique constraint holds this from the other side; the
    decision holds it here so that the requests are never spent in the
    first place.
    """
    last = RefreshRecord(watchlist="DOWN_TREND", refreshed_on=TODAY)

    decision = _decide("BREAKOUT_READY", last, on=TODAY)

    assert not decision.due
    assert decision.trigger == ALREADY_REFRESHED


def test_a_security_on_no_watchlist_is_never_due():
    """UNCLASSIFIED and DISTRIBUTION are on no list, so on no tier."""
    decision = _decide(None, None)

    assert not decision.due
    assert decision.trigger == NO_TIER


def test_a_recorded_tier_that_no_longer_exists_falls_back_to_interval_math():
    """A watchlist removed from Module 10 must not strand its securities.

    There is no escalation to detect against a tier that is gone, so the
    decision uses the current tier's interval and says nothing about the
    missing one.
    """
    last = RefreshRecord(watchlist="RETIRED_LIST", refreshed_on=TODAY)

    decision = _decide("CONSOLIDATION", last, on=TODAY + timedelta(days=10))

    assert decision.due
    assert decision.trigger == INTERVAL_ELAPSED


def test_every_decision_states_its_reasoning():
    """A run's decisions are logged; a blank reason is an unexplained skip."""
    cases = [
        _decide(None, None),
        _decide("DOWN_TREND", None),
        _decide("DOWN_TREND", RefreshRecord("DOWN_TREND", TODAY), on=TODAY + timedelta(days=1)),
        _decide("CONSOLIDATION", RefreshRecord("DOWN_TREND", TODAY), on=TODAY + timedelta(days=1)),
        _decide("DOWN_TREND", RefreshRecord("DOWN_TREND", TODAY), on=TODAY + timedelta(days=30)),
    ]

    for decision in cases:
        assert len(decision.reason) > 40, decision
