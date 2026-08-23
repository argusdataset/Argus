"""Writing setup events, and the sequence that makes their order a fact.

`setup_events` is append-only (Module 03's guard rejects UPDATE and
DELETE) and carries a `sequence_number` unique per setup. Both matter, for
different reasons:

**Append-only** is what makes "quietly rewrite a setup's history so a
failure looks like something else" impossible rather than discouraged.

**The sequence** is what makes the order a fact rather than an inference.
Module 03's comment says it exists "so the event order is unambiguous even
when two events share a timestamp", and that case is not hypothetical: a
retreat and the invalidation it triggers are observed in the same scan and
carry the same `occurred_at`. Ordering by time alone would leave which
came last up to the database's row order.

It matters more in batch replay. Module 17 replays historical dates, so
`occurred_at` runs backwards relative to `created_at` and neither column
alone orders the history correctly. The sequence does, because it is
assigned in the order events are appended to *that setup*, whichever mode
appended them.

## Event types are free text, deliberately

Like `pending_material_events.event_type` in Module 03's schema. The
`lifecycle_status` column is the closed domain — four values, and this
module is forbidden to invent a fifth. What *happened* is open: a new kind
of endpoint or a new reason for invalidation should be a new constant
here, not a migration. The constants below are the names this module has
agreed on so far.

## Sequence allocation is not race-proof, and the database says so

`_next_sequence` reads the current maximum and adds one. Two writers
appending to the same setup concurrently can read the same maximum, and
the second insert then violates `uq_setup_event_sequence` — which is the
correct outcome: the write fails loudly rather than silently producing two
events claiming the same position. A caller that expects concurrency
should retry. Serialising here instead would need a lock this module has
no business taking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from infra.db.enums import SetupLifecycleStatus
from infra.db.schema.setups import setup_events

# --------------------------------------------------------------------------
# Event types. Free text in the schema; named constants here.
# --------------------------------------------------------------------------

#: A setup was opened because Module 10 placed the security in a
#: pattern-relevant state.
DETECTED = "detected"
#: It cleared the qualification bar and became actively tracked.
QUALIFIED = "qualified"
#: Tracking began in earnest — the structure advanced toward expansion.
ACTIVATED = "activated"
#: Module 10 recorded a retreat while the setup was ACTIVE. Information,
#: not a demotion — see `derivation.py`.
RETREAT = "market_state_retreat"
#: Module 13 gated the candidate because it lost eligibility it had held.
INVALIDATED_LOST_ELIGIBILITY = "invalidated_lost_eligibility"
#: Module 13 gated it on Module 09's current verdict.
INVALIDATED_INELIGIBLE = "invalidated_ineligible"
#: The structure resolved — the security left the base one way or another.
ENDPOINT_REACHED = "endpoint_reached"
#: The tracking window closed with no resolution.
EXPIRED_ACTIVE = "expired_active"
#: A setup sat detected but never qualified for longer than the window.
EXPIRED_UNQUALIFIED = "expired_unqualified"

EVENT_TYPES: tuple[str, ...] = (
    DETECTED,
    QUALIFIED,
    ACTIVATED,
    RETREAT,
    INVALIDATED_LOST_ELIGIBILITY,
    INVALIDATED_INELIGIBLE,
    ENDPOINT_REACHED,
    EXPIRED_ACTIVE,
    EXPIRED_UNQUALIFIED,
)

#: Event types that put a setup at its endpoint. Module 15 decides *which*
#: outcome; this module only recognises that one is due.
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        INVALIDATED_LOST_ELIGIBILITY,
        INVALIDATED_INELIGIBLE,
        ENDPOINT_REACHED,
        EXPIRED_ACTIVE,
        EXPIRED_UNQUALIFIED,
    }
)


@dataclass(frozen=True, slots=True)
class SetupEvent:
    """One recorded lifecycle event."""

    setup_id: UUID
    sequence_number: int
    lifecycle_status: SetupLifecycleStatus
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    id: UUID | None = None

    @property
    def is_terminal(self) -> bool:
        return self.event_type in TERMINAL_EVENT_TYPES

    def as_dict(self) -> dict[str, Any]:
        return {
            "setup_id": str(self.setup_id),
            "sequence_number": self.sequence_number,
            "lifecycle_status": self.lifecycle_status.value,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": dict(self.payload),
        }


def append_event(
    connection: Connection,
    setup_id: UUID,
    *,
    lifecycle_status: SetupLifecycleStatus,
    event_type: str,
    occurred_at: datetime,
    payload: dict[str, Any] | None = None,
) -> SetupEvent:
    """Append one event to a setup's history.

    `occurred_at` is a plain argument, per `CROSS_CUTTING_REQUIREMENTS.md`:
    the same call records an event happening now or one being replayed for
    2015, and nothing here reads a wall clock to decide which.
    """
    sequence = _next_sequence(connection, setup_id)
    body = dict(payload or {})
    body.setdefault("calibration_status", "UNVALIDATED_PLACEHOLDERS")

    event_id = connection.execute(
        setup_events.insert()
        .values(
            setup_id=setup_id,
            sequence_number=sequence,
            lifecycle_status=lifecycle_status.value,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=body,
        )
        .returning(setup_events.c.id)
    ).scalar_one()

    return SetupEvent(
        setup_id=setup_id,
        sequence_number=sequence,
        lifecycle_status=lifecycle_status,
        event_type=event_type,
        occurred_at=occurred_at,
        payload=body,
        id=event_id,
    )


def _next_sequence(connection: Connection, setup_id: UUID) -> int:
    """The next position in this setup's history.

    Starts at zero, per Module 03's `sequence_non_negative` check. See the
    module docstring on why this is deliberately not locked.
    """
    highest = connection.execute(
        select(func.max(setup_events.c.sequence_number)).where(setup_events.c.setup_id == setup_id)
    ).scalar_one_or_none()
    return 0 if highest is None else int(highest) + 1
