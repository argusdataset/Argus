"""Turning Module 10's states and Module 13's verdicts into lifecycle events.

## Detection does not require a score, and that is the module's load-bearing decision

The tempting rule is "a setup is created when a candidate is scored".
Applied today it creates nothing, ever, and the reason is a loop rather
than a threshold:

```
no setups -> no outcomes (Module 15) -> no historical cases
          -> Module 11 returns INSUFFICIENT for everything
          -> Module 13 refuses to score anything
          -> no setups
```

Module 13's realistic run produced twelve `INSUFFICIENT_EVIDENCE` results
and zero scores, correctly. If a score were the price of admission, ARGUS
would never open a setup, never record an outcome, and Module 17 would
have nothing to calibrate against — permanently. The system could not
bootstrap itself.

So **detection is driven by Module 10's state assignment**, matching
Module 03's own comment on `setups` ("A setup is created when a candidate
reaches CONSOLIDATION"). Module 13's score gates **qualification**, which
is the point where ARGUS commits to tracking something rather than merely
noticing it. Today every setup sits at DETECTION, which is the honest
picture: ARGUS has noticed these bases and cannot yet say anything about
them.

## Two state machines, not one

A security's market state moves both ways and Module 10 records every
move. A setup's lifecycle status answers a different question — how far
ARGUS has committed — and runs forward only. A retreat while ACTIVE is
recorded as an event *at* ACTIVE, carrying Module 10's own backward count,
rather than demoting the setup. See `derivation.py`.

## UNCLASSIFIED never ends a setup

Module 10 established that UNCLASSIFIED sits outside the cycle: a security
that stops being classifiable has had a change in what is *knowable*, not
a retreat. A feature vector missing for one scan would otherwise close
setups that are perfectly alive, and closing one is irreversible.

## Dual mode

`as_of` is a plain argument throughout and nothing reads a wall clock.
The same call advances a live scan or replays 2015, which is what Module
17 will do — and the sequence numbers make the resulting history order
correctly either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.lifecycle.config import LifecycleConfig
from core.lifecycle.derivation import LifecycleState, advance, open_setups
from core.lifecycle.events import (
    ACTIVATED,
    DETECTED,
    ENDPOINT_REACHED,
    EXPIRED_ACTIVE,
    EXPIRED_UNQUALIFIED,
    INVALIDATED_INELIGIBLE,
    INVALIDATED_LOST_ELIGIBILITY,
    QUALIFIED,
    RETREAT,
    SetupEvent,
    append_event,
)
from core.scoring.engine import Lineage, ScoredSignal
from core.scoring.gating import ScoringDecision
from infra.db.enums import MarketState, SetupLifecycleStatus
from infra.db.schema.setups import setups

#: States in which a base is worth opening a setup for. Module 03's schema
#: comment names CONSOLIDATION; ACCUMULATION is the same base one phase
#: further along, and a security first seen there has not stopped being a
#: base for having been noticed late.
DETECTION_STATES: tuple[MarketState, ...] = (
    MarketState.CONSOLIDATION,
    MarketState.ACCUMULATION,
)

#: States that mean the structure has advanced far enough to track in
#: earnest.
ACTIVATION_STATES: tuple[MarketState, ...] = (
    MarketState.BREAKOUT_WATCH,
    MarketState.BREAKOUT_READY,
)

#: States that mean the setup resolved, one way or another. Module 15
#: decides which; this module only recognises that one is due.
#:
#: UNCLASSIFIED is deliberately absent — see the module docstring.
ENDPOINT_STATES: tuple[MarketState, ...] = (
    MarketState.UPTREND,
    MarketState.DISTRIBUTION,
    MarketState.DOWN_TREND,
)

# --------------------------------------------------------------------------
# What happened to each candidate
# --------------------------------------------------------------------------

OPENED = "opened"
QUALIFIED_ACTION = "qualified"
ACTIVATED_ACTION = "activated"
RETREAT_RECORDED = "retreat_recorded"
INVALIDATED = "invalidated"
ENDPOINT = "endpoint_recorded"
EXPIRED = "expired"
UNCHANGED = "unchanged"
#: Module 13 gated a candidate that has no open setup. Nothing is written
#: — see `LifecycleReport` and the module README on why.
GATED_NO_SETUP = "gated_no_setup"
#: Not in a pattern-relevant state and not already tracked.
NOT_TRACKED = "not_tracked"

ACTIONS: tuple[str, ...] = (
    OPENED,
    QUALIFIED_ACTION,
    ACTIVATED_ACTION,
    RETREAT_RECORDED,
    INVALIDATED,
    ENDPOINT,
    EXPIRED,
    UNCHANGED,
    GATED_NO_SETUP,
    NOT_TRACKED,
)

#: Which gating decision produces which invalidation event type.
_INVALIDATION_EVENTS: dict[ScoringDecision, str] = {
    ScoringDecision.GATED_LOST_ELIGIBILITY: INVALIDATED_LOST_ELIGIBILITY,
    ScoringDecision.GATED_INELIGIBLE: INVALIDATED_INELIGIBLE,
}


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    """One candidate as this scan sees it.

    `state` is Module 10's assignment, `signal` is Module 13's verdict —
    which may be a refusal or a gate, and usually is.
    `backward_transitions` is Module 10's own stored count, carried rather
    than recomputed; None means it was not read, and retreat detection is
    then skipped rather than guessed at.
    """

    security_id: UUID
    state: MarketState | None = None
    signal: ScoredSignal | None = None
    backward_transitions: int | None = None

    @classmethod
    def from_scoring(
        cls,
        signal: ScoredSignal,
        *,
        state: MarketState | None = None,
        backward_transitions: int | None = None,
    ) -> CandidateObservation:
        return cls(
            security_id=signal.security_id,
            state=state,
            signal=signal,
            backward_transitions=backward_transitions,
        )


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    """What this scan did about one candidate, and why."""

    security_id: UUID
    action: str
    setup_id: UUID | None = None
    status: SetupLifecycleStatus | None = None
    event: SetupEvent | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "security_id": str(self.security_id),
            "action": self.action,
            "setup_id": str(self.setup_id) if self.setup_id else None,
            "status": self.status.value if self.status else None,
            "event_type": self.event.event_type if self.event else None,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class LifecycleReport:
    """The outcome of advancing a whole candidate set."""

    as_of: datetime
    config_version: str
    results: list[LifecycleResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        tally = dict.fromkeys(ACTIONS, 0)
        for result in self.results:
            tally[result.action] += 1
        return tally

    def by_action(self, action: str) -> list[LifecycleResult]:
        return [result for result in self.results if result.action == action]

    def __len__(self) -> int:
        return len(self.results)


def advance_lifecycle(
    connection: Connection,
    observations: list[CandidateObservation],
    *,
    as_of: datetime,
    lineage: Lineage,
    config: LifecycleConfig | None = None,
) -> LifecycleReport:
    """Move every observed candidate's setup along, or open one, or neither.

    One transition per candidate per scan at most. A setup does not skip
    from DETECTION to ACTIVE in a single call even when both conditions
    hold: the intermediate event is the record that the bar was cleared,
    and a history missing it could not be audited afterwards.
    """
    config = config or LifecycleConfig()
    tracked = open_setups(connection, as_of=as_of)
    by_security = _index_by_security(connection, tracked)

    results = [
        _advance_one(
            connection,
            observation,
            state=by_security.get(observation.security_id),
            as_of=as_of,
            lineage=lineage,
            config=config,
        )
        for observation in observations
    ]
    return LifecycleReport(as_of=as_of, config_version=config.version_label(), results=results)


def open_setup(
    connection: Connection,
    security_id: UUID,
    *,
    as_of: datetime,
    lineage: Lineage,
    payload: dict[str, Any] | None = None,
) -> tuple[UUID, SetupEvent]:
    """Create a setup and its opening DETECTION event, in one step.

    The two are written together deliberately: a `setups` row with no
    events has no derivable status, so it would be a setup that exists and
    cannot be asked about.
    """
    setup_id = connection.execute(
        setups.insert()
        .values(
            security_id=security_id,
            detected_at=as_of,
            target_model_version_id=lineage.target_model_version_id,
            detection_configuration_id=lineage.detection_configuration_id,
            universe_version_id=lineage.universe_version_id,
        )
        .returning(setups.c.id)
    ).scalar_one()

    event = append_event(
        connection,
        setup_id,
        lifecycle_status=SetupLifecycleStatus.DETECTION,
        event_type=DETECTED,
        occurred_at=as_of,
        payload=payload or {},
    )
    return setup_id, event


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _advance_one(
    connection: Connection,
    observation: CandidateObservation,
    *,
    state: LifecycleState | None,
    as_of: datetime,
    lineage: Lineage,
    config: LifecycleConfig,
) -> LifecycleResult:
    signal = observation.signal
    decision = signal.decision if signal is not None else None

    if decision in _INVALIDATION_EVENTS:
        return _handle_gate(connection, observation, state=state, as_of=as_of)

    if state is None:
        return _maybe_open(connection, observation, as_of=as_of, lineage=lineage)

    expiry = _expired(state, as_of=as_of, config=config)
    if expiry is not None:
        return _terminate(connection, observation, state, as_of, *expiry)

    if observation.state in ENDPOINT_STATES:
        return _terminate(
            connection,
            observation,
            state,
            as_of,
            ENDPOINT_REACHED,
            f"Market state reached {observation.state.value}: the base resolved. "
            "Module 15 decides how.",
        )

    if state.status is SetupLifecycleStatus.DETECTION:
        return _maybe_qualify(connection, observation, state, as_of=as_of, config=config)

    if state.status is SetupLifecycleStatus.QUALIFICATION:
        return _maybe_activate(connection, observation, state, as_of=as_of)

    return _maybe_record_retreat(connection, observation, state, as_of=as_of)


def _handle_gate(
    connection: Connection,
    observation: CandidateObservation,
    *,
    state: LifecycleState | None,
    as_of: datetime,
) -> LifecycleResult:
    """Module 13 gated this candidate.

    With an open setup, that is an invalidation and belongs in the log.
    Without one, nothing is written — deliberately. Creating a setup in
    order to record that it should not exist would turn `setups` into a
    log of everything ARGUS ever looked at, once per scan per security
    across a ten-thousand-name universe. Module 09's
    `eligibility_check_results` already records durably that the candidate
    was evaluated and which gate rejected it, which is the same fact in
    the table built for it.
    """
    signal = observation.signal
    assert signal is not None and signal.decision in _INVALIDATION_EVENTS

    if state is None:
        return LifecycleResult(
            security_id=observation.security_id,
            action=GATED_NO_SETUP,
            reason=(
                f"{signal.decision.value} with no open setup: nothing to invalidate. "
                "Module 09's eligibility_check_results already records the rejection."
            ),
        )

    event = advance(
        connection,
        state.setup_id,
        to=SetupLifecycleStatus.OUTCOME,
        event_type=_INVALIDATION_EVENTS[signal.decision],
        occurred_at=as_of,
        payload=_invalidation_payload(observation, signal, state, as_of),
    )
    return LifecycleResult(
        security_id=observation.security_id,
        action=INVALIDATED,
        setup_id=state.setup_id,
        status=SetupLifecycleStatus.OUTCOME,
        event=event,
        reason=signal.verdict.reason if signal.verdict else signal.decision.value,
    )


def _invalidation_payload(
    observation: CandidateObservation,
    signal: ScoredSignal,
    state: LifecycleState,
    as_of: datetime,
) -> dict[str, Any]:
    """Everything Module 15 will need to classify this ending.

    Module 13's `verdict.detail` is carried through whole rather than
    summarised: it holds the eligibility trend, when the security last
    passed, and exactly which gates newly failed. Re-deriving any of that
    later would mean re-running Module 09 against data that has moved on.
    """
    verdict = signal.verdict
    return {
        "decision": signal.decision.value,
        "reason": verdict.reason if verdict else None,
        "verdict_detail": verdict.detail if verdict else {},
        "market_state": observation.state.value if observation.state else None,
        "backward_transitions": observation.backward_transitions,
        "status_before": state.status.value,
        # Measured to the scan's `as_of`, which is also this event's
        # `occurred_at`. Measuring to the signal's own `event_time`
        # instead would let a payload disagree with the timestamp of the
        # event carrying it whenever scoring and the lifecycle scan ran
        # at different instants.
        "tracked_for_days": _days_between(state.opened_at, as_of),
        "handoff": "Module 15 (outcome tracking)",
    }


def _maybe_open(
    connection: Connection,
    observation: CandidateObservation,
    *,
    as_of: datetime,
    lineage: Lineage,
) -> LifecycleResult:
    if observation.state not in DETECTION_STATES:
        return LifecycleResult(
            security_id=observation.security_id,
            action=NOT_TRACKED,
            reason=(
                f"Market state {observation.state.value if observation.state else 'unknown'} "
                "is not one a base is opened for."
            ),
        )

    setup_id, event = open_setup(
        connection,
        observation.security_id,
        as_of=as_of,
        lineage=lineage,
        payload=_observation_payload(observation),
    )
    return LifecycleResult(
        security_id=observation.security_id,
        action=OPENED,
        setup_id=setup_id,
        status=SetupLifecycleStatus.DETECTION,
        event=event,
        reason=(
            f"Module 10 placed this security in {observation.state.value}. Detection "
            "deliberately does not require a score — see core/lifecycle/engine.py."
        ),
    )


def _maybe_qualify(
    connection: Connection,
    observation: CandidateObservation,
    state: LifecycleState,
    *,
    as_of: datetime,
    config: LifecycleConfig,
) -> LifecycleResult:
    signal = observation.signal
    thresholds = config.thresholds

    if signal is None or signal.decision is not ScoringDecision.SCORED:
        return LifecycleResult(
            security_id=observation.security_id,
            action=UNCHANGED,
            setup_id=state.setup_id,
            status=state.status,
            reason=(
                "No score to qualify on. Expected for nearly every setup until "
                "Module 17 populates the historical case dataset; the setup stays "
                "tracked at DETECTION rather than being closed."
            ),
        )

    if (
        signal.argus_score < thresholds.min_argus_score.value
        or signal.confidence < thresholds.min_confidence.value
    ):
        return LifecycleResult(
            security_id=observation.security_id,
            action=UNCHANGED,
            setup_id=state.setup_id,
            status=state.status,
            reason=(
                f"Scored {signal.argus_score:.1f} at confidence {signal.confidence:.1f}; "
                f"the bar is {thresholds.min_argus_score.value:.1f} and "
                f"{thresholds.min_confidence.value:.1f}."
            ),
        )

    event = advance(
        connection,
        state.setup_id,
        to=SetupLifecycleStatus.QUALIFICATION,
        event_type=QUALIFIED,
        occurred_at=as_of,
        payload={
            **_observation_payload(observation),
            "argus_score": signal.argus_score,
            "confidence": signal.confidence,
            "opportunity_score": signal.opportunity_score,
            "risk_score": signal.risk_score,
            "thresholds": thresholds.as_dict(),
            "scoring_configuration_id": str(signal.lineage.scoring_configuration_id),
        },
    )
    return LifecycleResult(
        security_id=observation.security_id,
        action=QUALIFIED_ACTION,
        setup_id=state.setup_id,
        status=SetupLifecycleStatus.QUALIFICATION,
        event=event,
        reason=f"Cleared the qualification bar at {signal.argus_score:.1f}.",
    )


def _maybe_activate(
    connection: Connection,
    observation: CandidateObservation,
    state: LifecycleState,
    *,
    as_of: datetime,
) -> LifecycleResult:
    if observation.state not in ACTIVATION_STATES:
        return LifecycleResult(
            security_id=observation.security_id,
            action=UNCHANGED,
            setup_id=state.setup_id,
            status=state.status,
            reason="Qualified, waiting for the structure to advance.",
        )

    event = advance(
        connection,
        state.setup_id,
        to=SetupLifecycleStatus.ACTIVE,
        event_type=ACTIVATED,
        occurred_at=as_of,
        payload=_observation_payload(observation),
    )
    return LifecycleResult(
        security_id=observation.security_id,
        action=ACTIVATED_ACTION,
        setup_id=state.setup_id,
        status=SetupLifecycleStatus.ACTIVE,
        event=event,
        reason=f"Advanced to {observation.state.value}.",
    )


def _maybe_record_retreat(
    connection: Connection,
    observation: CandidateObservation,
    state: LifecycleState,
    *,
    as_of: datetime,
) -> LifecycleResult:
    """An ACTIVE setup whose security has retreated along Module 10's cycle.

    Recorded at ACTIVE rather than as a demotion. The count comes from
    Module 10's stored backward-transition flag, compared against the
    count this setup last recorded — so a retreat is detected from what
    Module 10 wrote at the time, never re-derived here.
    """
    observed = observation.backward_transitions
    previous = _last_backward_count(state)
    if observed is None or previous is None or observed <= previous:
        return LifecycleResult(
            security_id=observation.security_id,
            action=UNCHANGED,
            setup_id=state.setup_id,
            status=state.status,
            reason=(
                "Active, no new retreat recorded."
                if observed is not None and previous is not None
                else "Active; Module 10's backward count was not supplied, so no "
                "retreat comparison was made."
            ),
        )

    event = advance(
        connection,
        state.setup_id,
        to=SetupLifecycleStatus.ACTIVE,
        event_type=RETREAT,
        occurred_at=as_of,
        payload={
            **_observation_payload(observation),
            "backward_transitions_before": previous,
            "note": (
                "A retreat is normal and is recorded inside ACTIVE. The lifecycle "
                "runs forward only — see core/lifecycle/derivation.py."
            ),
        },
    )
    return LifecycleResult(
        security_id=observation.security_id,
        action=RETREAT_RECORDED,
        setup_id=state.setup_id,
        status=SetupLifecycleStatus.ACTIVE,
        event=event,
        reason=f"Backward transitions rose from {previous} to {observed}.",
    )


def _terminate(
    connection: Connection,
    observation: CandidateObservation,
    state: LifecycleState,
    as_of: datetime,
    event_type: str,
    reason: str,
) -> LifecycleResult:
    event = advance(
        connection,
        state.setup_id,
        to=SetupLifecycleStatus.OUTCOME,
        event_type=event_type,
        occurred_at=as_of,
        payload={
            **_observation_payload(observation),
            "status_before": state.status.value,
            "reason": reason,
            "tracked_for_days": _days_between(state.opened_at, as_of),
            "handoff": "Module 15 (outcome tracking)",
        },
    )
    action = EXPIRED if event_type in (EXPIRED_ACTIVE, EXPIRED_UNQUALIFIED) else ENDPOINT
    return LifecycleResult(
        security_id=observation.security_id,
        action=action,
        setup_id=state.setup_id,
        status=SetupLifecycleStatus.OUTCOME,
        event=event,
        reason=reason,
    )


def _expired(
    state: LifecycleState, *, as_of: datetime, config: LifecycleConfig
) -> tuple[str, str] | None:
    """Whether this setup's window has closed, and which window it was."""
    thresholds = config.thresholds
    if state.status is SetupLifecycleStatus.ACTIVE:
        if as_of - state.entered_at > thresholds.active_window:
            return (
                EXPIRED_ACTIVE,
                f"Active for more than {thresholds.max_active_duration_days.value:.0f} "
                "days without resolving.",
            )
        return None

    if as_of - state.opened_at > thresholds.detection_window:
        return (
            EXPIRED_UNQUALIFIED,
            f"Tracked for more than {thresholds.max_detection_duration_days.value:.0f} "
            "days without reaching ACTIVE.",
        )
    return None


def _observation_payload(observation: CandidateObservation) -> dict[str, Any]:
    signal = observation.signal
    return {
        "market_state": observation.state.value if observation.state else None,
        "backward_transitions": observation.backward_transitions,
        "evidence_status": (
            signal.evidence_status.value
            if signal is not None and signal.evidence_status is not None
            else None
        ),
        "decision": signal.decision.value if signal is not None else None,
    }


def _last_backward_count(state: LifecycleState) -> int | None:
    value = state.latest_event.payload.get("backward_transitions")
    return None if value is None else int(value)


def _days_between(start: datetime, end: datetime) -> float:
    return (end - start) / timedelta(days=1)


def _index_by_security(
    connection: Connection, tracked: dict[UUID, LifecycleState]
) -> dict[UUID, LifecycleState]:
    """Open setups keyed by security rather than by setup.

    One open setup per security is the invariant this module maintains:
    a second one would make "which setup does this candidate belong to"
    ambiguous at every later scan. Where two exist, the most recently
    opened wins and the situation is visible in the log rather than
    silently resolved.
    """
    if not tracked:
        return {}
    rows = connection.execute(
        select(setups.c.id, setups.c.security_id).where(setups.c.id.in_(list(tracked)))
    ).all()
    by_security: dict[UUID, LifecycleState] = {}
    for row in rows:
        state = tracked[row.id]
        existing = by_security.get(row.security_id)
        if existing is None or state.opened_at > existing.opened_at:
            by_security[row.security_id] = state
    return by_security
