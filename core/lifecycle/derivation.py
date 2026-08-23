"""Deriving a setup's status from its events, and why it is never stored.

Module 03's schema has no `status` column on `setups`, and its comment
says why: "Adding one here would reintroduce the mutable field the event
sourcing exists to avoid." This module is the other half of that decision
— the place the status is worked out instead.

## Ordered by sequence, never by time

The obvious derivation is "read the latest row". It is wrong twice over,
and both cases occur in practice rather than in theory:

* **Same-timestamp events.** A retreat and the invalidation it triggers
  are observed in one scan and share an `occurred_at`. Ordering by time
  leaves which came last to the database's row order, so the derived
  status flips between ACTIVE and OUTCOME on re-read.
* **Batch replay.** Module 17 replays historical dates, so `created_at`
  runs forward while `occurred_at` runs backwards across a replay. Neither
  column alone orders one setup's history correctly; the sequence, which
  is assigned per setup in append order, does.

So `current_status` orders by `sequence_number` and nothing else, and
there is a test that fails if it is changed back to a timestamp.

## Monotonic forward, with retreats recorded inside a status

The lifecycle runs DETECTION → QUALIFICATION → ACTIVE → OUTCOME and never
runs backwards. That is deliberate and worth defending, because Module 10
established that backward movement is first-class and this looks like the
opposite rule.

It is not. The two state machines answer different questions. A security's
market state moves both ways and Module 10 records every move. A setup's
lifecycle status answers "how far has ARGUS committed to tracking this",
and that only moves one way: a setup that reached ACTIVE has been active,
and demoting it to QUALIFICATION would make that unanswerable from the
current status while erasing nothing from the log.

A retreat while ACTIVE is therefore recorded as an event **at** ACTIVE —
present in the history, queryable, carrying Module 10's own backward
count — rather than as a demotion. If the retreat goes far enough to cost
the security its eligibility, Module 13 gates it and the setup moves to
OUTCOME, which is forward, not back.

`advance()` enforces the monotonicity, so a caller cannot write a
regressing event by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.lifecycle.events import SetupEvent, append_event
from infra.db.enums import SetupLifecycleStatus
from infra.db.schema.setups import setup_events, setups

#: The nominal forward order. A setup moves along it or stays put.
LIFECYCLE_ORDER: tuple[SetupLifecycleStatus, ...] = (
    SetupLifecycleStatus.DETECTION,
    SetupLifecycleStatus.QUALIFICATION,
    SetupLifecycleStatus.ACTIVE,
    SetupLifecycleStatus.OUTCOME,
)


class LifecycleRegression(ValueError):
    """An attempt to move a setup backwards along the lifecycle."""


@dataclass(frozen=True, slots=True)
class LifecycleState:
    """A setup's status at one instant, and the history behind it."""

    setup_id: UUID
    status: SetupLifecycleStatus
    #: When the setup was opened — the first event's `occurred_at`.
    #: Carried because "how long has this been tracked" and "how long has
    #: it been in this status" are different questions and both get asked.
    opened_at: datetime
    #: When the setup entered its current status — the `occurred_at` of
    #: the earliest event carrying it, not of the latest event overall.
    entered_at: datetime
    #: Position of the event the status was read from.
    sequence_number: int
    events_observed: int
    #: The event that put the setup here.
    latest_event: SetupEvent

    @property
    def is_terminal(self) -> bool:
        return self.status is SetupLifecycleStatus.OUTCOME

    @property
    def is_open(self) -> bool:
        return not self.is_terminal

    def as_dict(self) -> dict[str, Any]:
        return {
            "setup_id": str(self.setup_id),
            "status": self.status.value,
            "opened_at": self.opened_at.isoformat(),
            "entered_at": self.entered_at.isoformat(),
            "sequence_number": self.sequence_number,
            "events_observed": self.events_observed,
            "latest_event_type": self.latest_event.event_type,
            "is_terminal": self.is_terminal,
        }


def history(
    connection: Connection,
    setup_id: UUID,
    *,
    as_of: datetime | None = None,
) -> list[SetupEvent]:
    """Every event for one setup, in sequence order.

    `as_of` bounds the history to what had happened by that instant — the
    same plain argument the rest of ARGUS uses, so a replay asking "what
    was this setup in March" does not see April's events.
    """
    query = select(setup_events).where(setup_events.c.setup_id == setup_id)
    if as_of is not None:
        query = query.where(setup_events.c.occurred_at <= as_of)

    rows = connection.execute(query.order_by(setup_events.c.sequence_number)).all()
    return [
        SetupEvent(
            setup_id=row.setup_id,
            sequence_number=row.sequence_number,
            lifecycle_status=SetupLifecycleStatus(row.lifecycle_status),
            event_type=row.event_type,
            occurred_at=row.occurred_at,
            payload=dict(row.payload or {}),
            id=row.id,
        )
        for row in rows
    ]


def current_status(
    connection: Connection,
    setup_id: UUID,
    *,
    as_of: datetime | None = None,
) -> LifecycleState | None:
    """Where this setup stands, derived from its events.

    None when the setup has no events knowable by `as_of` — which is a
    real answer during a replay of a date before it was detected, not an
    error.
    """
    events = history(connection, setup_id, as_of=as_of)
    return state_from(events)


def state_from(events: list[SetupEvent]) -> LifecycleState | None:
    """The derivation itself, over an already-loaded history.

    Separated from the query so the ordering rule can be tested against a
    hand-built history with no database in the way, and so a caller that
    already holds the events does not re-read them.
    """
    if not events:
        return None

    ordered = sorted(events, key=lambda event: event.sequence_number)
    latest = ordered[-1]
    entered = next(
        event.occurred_at for event in ordered if event.lifecycle_status is latest.lifecycle_status
    )
    return LifecycleState(
        setup_id=latest.setup_id,
        status=latest.lifecycle_status,
        opened_at=ordered[0].occurred_at,
        entered_at=entered,
        sequence_number=latest.sequence_number,
        events_observed=len(ordered),
        latest_event=latest,
    )


def advance(
    connection: Connection,
    setup_id: UUID,
    *,
    to: SetupLifecycleStatus,
    event_type: str,
    occurred_at: datetime,
    payload: dict[str, Any] | None = None,
) -> SetupEvent:
    """Append an event, refusing to move the setup backwards.

    Staying at the current status is allowed and is how a retreat, or any
    other observation worth recording, is written. Moving to an earlier
    status is refused: the log would still hold the whole history, but the
    derived status would start answering a different question, and every
    consumer reads the derived status.
    """
    state = current_status(connection, setup_id)
    if state is not None and _position(to) < _position(state.status):
        raise LifecycleRegression(
            f"Setup {setup_id} is {state.status.value}; refusing to move it back to "
            f"{to.value}. A retreat is recorded as an event at the current status — "
            "see core/lifecycle/derivation.py."
        )
    return append_event(
        connection,
        setup_id,
        lifecycle_status=to,
        event_type=event_type,
        occurred_at=occurred_at,
        payload=payload,
    )


def open_setups(
    connection: Connection,
    *,
    security_id: UUID | None = None,
    as_of: datetime | None = None,
) -> dict[UUID, LifecycleState]:
    """Every setup not yet at OUTCOME, by setup id.

    One query for the events of every candidate setup rather than one per
    setup — the batching discipline Modules 08 to 13 follow. A scan asks
    this once and then decides per candidate.
    """
    query = select(setup_events, setups.c.security_id).join(
        setups, setups.c.id == setup_events.c.setup_id
    )
    if security_id is not None:
        query = query.where(setups.c.security_id == security_id)
    if as_of is not None:
        query = query.where(setup_events.c.occurred_at <= as_of)

    grouped: dict[UUID, list[SetupEvent]] = {}
    for row in connection.execute(query.order_by(setup_events.c.sequence_number)):
        grouped.setdefault(row.setup_id, []).append(
            SetupEvent(
                setup_id=row.setup_id,
                sequence_number=row.sequence_number,
                lifecycle_status=SetupLifecycleStatus(row.lifecycle_status),
                event_type=row.event_type,
                occurred_at=row.occurred_at,
                payload=dict(row.payload or {}),
                id=row.id,
            )
        )

    states = {}
    for candidate, events in grouped.items():
        state = state_from(events)
        if state is not None and state.is_open:
            states[candidate] = state
    return states


def _position(status: SetupLifecycleStatus) -> int:
    return LIFECYCLE_ORDER.index(status)
