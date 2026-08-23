"""Deriving status from a history, with no database in the way.

`state_from` is separated from the query for exactly this reason: the
ordering rule is the thing most likely to be got wrong, and it should be
testable against a hand-built history where the trap is visible in the
fixture rather than buried in a fixture's SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from core.lifecycle.derivation import LIFECYCLE_ORDER, state_from
from core.lifecycle.events import (
    ACTIVATED,
    DETECTED,
    ENDPOINT_REACHED,
    QUALIFIED,
    RETREAT,
    SetupEvent,
)
from infra.db.enums import SetupLifecycleStatus

START = datetime(2024, 1, 1, tzinfo=UTC)
SETUP = uuid4()


def _event(sequence: int, status: SetupLifecycleStatus, event_type: str, *, day: int):
    return SetupEvent(
        setup_id=SETUP,
        sequence_number=sequence,
        lifecycle_status=status,
        event_type=event_type,
        occurred_at=START + timedelta(days=day),
        payload={},
    )


def _progression() -> list[SetupEvent]:
    return [
        _event(0, SetupLifecycleStatus.DETECTION, DETECTED, day=0),
        _event(1, SetupLifecycleStatus.QUALIFICATION, QUALIFIED, day=10),
        _event(2, SetupLifecycleStatus.ACTIVE, ACTIVATED, day=20),
    ]


def test_an_empty_history_has_no_status():
    """Not DETECTION-by-default: a setup with no events has not been
    observed, which is different from having been detected."""
    assert state_from([]) is None


def test_the_status_is_the_highest_sequence_events_status():
    state = state_from(_progression())

    assert state.status is SetupLifecycleStatus.ACTIVE
    assert state.sequence_number == 2
    assert state.events_observed == 3


def test_entered_at_is_when_the_status_began_not_when_the_last_event_landed():
    """Two questions that look alike: how long has this been tracked, and
    how long has it been in this status. Both get asked — the expiry rules
    use one each — so both are carried."""
    events = _progression()
    events.append(_event(3, SetupLifecycleStatus.ACTIVE, RETREAT, day=25))

    state = state_from(events)

    assert state.entered_at == START + timedelta(days=20)
    assert state.opened_at == START
    assert state.latest_event.event_type == RETREAT


def test_ordering_by_sequence_not_by_time_decides_a_same_timestamp_history():
    """The load-bearing case, and it is not hypothetical.

    A retreat and the invalidation it triggers are observed in one scan
    and carry the same `occurred_at`. Sorting by time leaves which came
    last to whatever order the rows arrive in; sorting by sequence does
    not. This history is built so a naive read returns ACTIVE and the
    correct one returns OUTCOME.
    """
    events = _progression()
    events.append(_event(3, SetupLifecycleStatus.ACTIVE, RETREAT, day=25))
    events.append(_event(4, SetupLifecycleStatus.OUTCOME, ENDPOINT_REACHED, day=25))

    assert state_from(events).status is SetupLifecycleStatus.OUTCOME
    # And in any arrival order. This is the assertion that actually bites:
    # sorting by `occurred_at` is a *stable* sort, so it happens to give
    # the right answer when the rows arrive already in sequence order and
    # the wrong one when they do not. Feeding them backwards is what makes
    # the difference between the two orderings visible at all.
    assert state_from(list(reversed(events))).status is SetupLifecycleStatus.OUTCOME

    # What "latest by occurred_at" produces: the two tie, so the answer is
    # whichever row the database happened to hand back first. Both are
    # reachable, which is the defect — not that it is always wrong, but
    # that it is not always right and nothing says which.
    def naive(rows):
        return max(rows, key=lambda event: event.occurred_at).lifecycle_status

    assert naive(events) is SetupLifecycleStatus.ACTIVE
    assert naive(list(reversed(events))) is SetupLifecycleStatus.OUTCOME


def test_the_derivation_is_independent_of_the_order_rows_arrive_in():
    """A query without an ORDER BY, or a set-returning join, must not be
    able to change the answer."""
    events = _progression()
    forward = state_from(events)
    shuffled = state_from(list(reversed(events)))

    assert forward.status is shuffled.status
    assert forward.sequence_number == shuffled.sequence_number
    assert forward.entered_at == shuffled.entered_at


def test_a_retreat_inside_active_does_not_change_the_status():
    """Module 10 established backward movement is first-class. Here it is
    information recorded at ACTIVE, not a demotion — the two state
    machines answer different questions."""
    events = _progression()
    events.append(_event(3, SetupLifecycleStatus.ACTIVE, RETREAT, day=30))

    state = state_from(events)

    assert state.status is SetupLifecycleStatus.ACTIVE
    assert state.latest_event.event_type == RETREAT
    assert state.is_open


def test_reaching_outcome_makes_the_setup_terminal():
    events = _progression()
    events.append(_event(3, SetupLifecycleStatus.OUTCOME, ENDPOINT_REACHED, day=40))

    state = state_from(events)

    assert state.is_terminal
    assert not state.is_open
    assert state.latest_event.is_terminal


def test_the_lifecycle_order_is_the_four_states_and_nothing_else():
    """The brief forbids inventing a fifth. Asserted rather than trusted,
    because a new enum value would otherwise silently acquire a position."""
    assert LIFECYCLE_ORDER == (
        SetupLifecycleStatus.DETECTION,
        SetupLifecycleStatus.QUALIFICATION,
        SetupLifecycleStatus.ACTIVE,
        SetupLifecycleStatus.OUTCOME,
    )
    assert set(LIFECYCLE_ORDER) == set(SetupLifecycleStatus)


@pytest.mark.parametrize("status", list(SetupLifecycleStatus))
def test_every_status_has_exactly_one_position(status):
    assert LIFECYCLE_ORDER.count(status) == 1
