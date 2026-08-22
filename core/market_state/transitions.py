"""Recording state changes, and querying the history they build up.

## Backward transitions are first-class

A failed breakout returns a security from `BREAKOUT_READY` to
`CONSOLIDATION`. That is normal, expected, and carries information — it is
not an error, a correction, or a state to be smoothed away.

This matters more than it first appears. A security that has cycled
through `BREAKOUT_WATCH` three times before finally clearing the level is
a materially different thing from one that broke out on its first attempt,
and the only place that difference is recorded is here. Nothing in this
module tries to *use* that fact — computing anything predictive from it is
Module 11's job — but the history has to be queryable, correct, and
complete for Module 11 to have anything to work with.

So `cycle_count()` and `history()` exist now, before anything consumes
them, and are tested now.

## Append-only, with a derived projection

`market_state_transitions` is the authority: one immutable row per change,
carrying the duration spent in the prior state. `market_state` is a
projection of "where is this security now", updated in place, and Module
03's comment on the table says exactly that.

The projection is rebuildable from the log; the log is not rebuildable
from the projection. When they disagree, the log is right.

## Only changes are recorded

Re-running a classification that produces the same state writes nothing.
A transition row means "this security changed state", and emitting one per
scan would turn the history into a sampling log — `cycle_count()` would
count scans rather than cycles, and the one question the table exists to
answer would become unanswerable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.market_state.classifier import ClassificationResult
from infra.db.enums import MarketState
from infra.db.schema.intelligence import market_state, market_state_transitions


@dataclass(frozen=True, slots=True)
class StoredState:
    """A security's current state, as the projection holds it."""

    security_id: UUID
    state: MarketState
    entered_at: datetime
    confidence: float | None


@dataclass(frozen=True, slots=True)
class Transition:
    """One recorded state change."""

    security_id: UUID
    from_state: MarketState | None
    to_state: MarketState
    transition_time: datetime
    duration_in_prior_state: timedelta | None
    confidence: float | None

    @property
    def is_backward(self) -> bool:
        """Did this move the security earlier in the nominal cycle?

        Defined against `CYCLE_ORDER` rather than by listing the specific
        retreats, so a new state added to the enum does not silently
        acquire a wrong answer.
        """
        if self.from_state is None:
            return False
        origin = _cycle_position(self.from_state)
        destination = _cycle_position(self.to_state)
        if origin is None or destination is None:
            # UNCLASSIFIED is outside the cycle. A security losing
            # eligibility is a change in what is *knowable*, not a retreat
            # along the setup, and counting it as a failed breakout would
            # corrupt the very statistic Module 11 wants.
            return False
        return destination < origin


#: The nominal forward cycle, for classifying a transition's direction.
#: UNCLASSIFIED sits outside it — moving to or from UNCLASSIFIED is
#: neither forward nor backward, it is a change in what is knowable.
CYCLE_ORDER: tuple[MarketState, ...] = (
    MarketState.DOWN_TREND,
    MarketState.BASE_FORMING,
    MarketState.CONSOLIDATION,
    MarketState.ACCUMULATION,
    MarketState.BREAKOUT_WATCH,
    MarketState.BREAKOUT_READY,
    MarketState.UPTREND,
    MarketState.DISTRIBUTION,
)


def _cycle_position(state: MarketState) -> int | None:
    """Position along the nominal cycle, or None for states outside it.

    None rather than a sentinel integer: -1 would compare as "earlier than
    everything" and silently make every move to UNCLASSIFIED look like a
    retreat.
    """
    try:
        return CYCLE_ORDER.index(state)
    except ValueError:
        return None


def load_current_states(
    connection: Connection, security_ids: list[UUID]
) -> dict[UUID, StoredState]:
    """The projection for a batch, in one query."""
    if not security_ids:
        return {}
    rows = connection.execute(
        select(market_state).where(market_state.c.security_id.in_(security_ids))
    ).all()
    return {
        row.security_id: StoredState(
            security_id=row.security_id,
            state=MarketState(row.state),
            entered_at=row.entered_at,
            confidence=float(row.confidence) if row.confidence is not None else None,
        )
        for row in rows
    }


def record_transitions(
    connection: Connection,
    result: ClassificationResult,
) -> list[Transition]:
    """Write a transition row for every security whose state changed.

    Two queries plus two writes for the whole batch, regardless of size.
    Returns the transitions actually recorded, so a caller can act on them
    without re-reading.

    Requires `target_model_version_id`: Module 03 makes it `NOT NULL` on
    both tables, and for the same reason Module 08 refuses to write an
    unstamped feature vector — a state whose thresholds cannot be
    recovered is not reproducible, and these thresholds in particular are
    placeholders that *will* change.
    """
    if result.target_model_version_id is None:
        raise ValueError(
            "Cannot record transitions without a target_model_version_id: the "
            "thresholds that produced these states are unvalidated placeholders "
            "and must stay attributable. Call publish_target_model_version() first."
        )

    security_ids = list(result.assignments)
    if not security_ids:
        return []

    current = load_current_states(connection, security_ids)
    transitions: list[Transition] = []

    for security_id, assignment in result.assignments.items():
        stored = current.get(security_id)
        if stored is not None and stored.state is assignment.state:
            continue  # No change. See the module docstring.

        transitions.append(
            Transition(
                security_id=security_id,
                from_state=stored.state if stored else None,
                to_state=assignment.state,
                transition_time=result.as_of,
                duration_in_prior_state=(result.as_of - stored.entered_at if stored else None),
                confidence=assignment.confidence,
            )
        )

    if not transitions:
        return []

    _write_transitions(connection, result, transitions)
    _update_projection(connection, result, transitions)
    return transitions


def _write_transitions(
    connection: Connection, result: ClassificationResult, transitions: list[Transition]
) -> None:
    connection.execute(
        market_state_transitions.insert(),
        [
            {
                "security_id": t.security_id,
                "from_state": t.from_state.value if t.from_state else None,
                "to_state": t.to_state.value,
                "transition_time": t.transition_time,
                "duration_in_prior_state": t.duration_in_prior_state,
                "confidence": t.confidence,
                "evidence": _evidence_for(result, t),
                "target_model_version_id": result.target_model_version_id,
            }
            for t in transitions
        ],
    )


def _evidence_for(result: ClassificationResult, transition: Transition) -> dict:
    evidence = dict(result.assignments[transition.security_id].evidence)
    # Recorded rather than derived on read: `is_backward` depends on
    # CYCLE_ORDER, and a future reordering would silently reinterpret
    # every historical row.
    evidence["backward_transition"] = transition.is_backward
    return evidence


def _update_projection(
    connection: Connection, result: ClassificationResult, transitions: list[Transition]
) -> None:
    """Upsert `market_state` — the projection, not the authority."""
    statement = insert(market_state).values(
        [
            {
                "security_id": t.security_id,
                "state": t.to_state.value,
                "entered_at": t.transition_time,
                "confidence": t.confidence,
                "target_model_version_id": result.target_model_version_id,
                "updated_at": t.transition_time,
            }
            for t in transitions
        ]
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=["security_id"],
            set_={
                "state": statement.excluded.state,
                "entered_at": statement.excluded.entered_at,
                "confidence": statement.excluded.confidence,
                "target_model_version_id": statement.excluded.target_model_version_id,
                "updated_at": statement.excluded.updated_at,
            },
        )
    )


# --------------------------------------------------------------------------
# History queries — built for Module 11
# --------------------------------------------------------------------------


def history(
    connection: Connection,
    security_id: UUID,
    *,
    as_of: datetime | None = None,
) -> list[Transition]:
    """Every transition for one security, oldest first.

    `as_of` bounds the history to what was knowable at that instant. It is
    a plain argument for the same reason it is everywhere else: a
    historical replay asking "how many times had this cycled *by 2015*"
    must not see 2016's transitions.
    """
    query = select(market_state_transitions).where(
        market_state_transitions.c.security_id == security_id
    )
    if as_of is not None:
        query = query.where(market_state_transitions.c.transition_time <= as_of)

    rows = connection.execute(query.order_by(market_state_transitions.c.transition_time)).all()
    return [
        Transition(
            security_id=row.security_id,
            from_state=MarketState(row.from_state) if row.from_state else None,
            to_state=MarketState(row.to_state),
            transition_time=row.transition_time,
            duration_in_prior_state=row.duration_in_prior_state,
            confidence=float(row.confidence) if row.confidence is not None else None,
        )
        for row in rows
    ]


def cycle_count(
    connection: Connection,
    security_id: UUID,
    state: MarketState,
    *,
    as_of: datetime | None = None,
) -> int:
    """How many times this security has *entered* `state`.

    The question Module 11 will ask: a security on its third pass through
    `BREAKOUT_WATCH` is a different proposition from one on its first.

    Counts entries rather than distinct occupancy periods, which are the
    same thing here because a transition row is only written on an actual
    change — see the module docstring on why re-running a scan writes
    nothing.
    """
    query = (
        select(func.count())
        .select_from(market_state_transitions)
        .where(
            market_state_transitions.c.security_id == security_id,
            market_state_transitions.c.to_state == state.value,
        )
    )
    if as_of is not None:
        query = query.where(market_state_transitions.c.transition_time <= as_of)
    return int(connection.execute(query).scalar_one())


def backward_transition_count(
    connection: Connection,
    security_id: UUID,
    *,
    as_of: datetime | None = None,
) -> int:
    """How many times this security has retreated along the cycle.

    Reads the `backward_transition` flag recorded at write time rather
    than recomputing from `CYCLE_ORDER`, so a future reordering of the
    cycle cannot retroactively change what a historical row meant.
    """
    query = (
        select(func.count())
        .select_from(market_state_transitions)
        .where(
            market_state_transitions.c.security_id == security_id,
            market_state_transitions.c.evidence["backward_transition"].astext == "true",
        )
    )
    if as_of is not None:
        query = query.where(market_state_transitions.c.transition_time <= as_of)
    return int(connection.execute(query).scalar_one())
