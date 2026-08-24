"""The event log as the source of truth: ordering, immutability, replay."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from core.lifecycle.config import (
    CALIBRATABLE,
    LifecycleConfig,
    LifecycleThreshold,
    QualificationThresholds,
)
from core.lifecycle.derivation import current_status, history, open_setups
from core.lifecycle.engine import (
    ENDPOINT,
    EXPIRED,
    OPENED,
    CandidateObservation,
    advance_lifecycle,
    open_setup,
)
from core.lifecycle.events import (
    ENDPOINT_REACHED,
    EXPIRED_ACTIVE,
    EXPIRED_UNQUALIFIED,
    RETREAT,
    append_event,
)
from infra.db.enums import MarketState, SetupLifecycleStatus
from infra.db.schema.setups import setup_events, setups
from tests.integration.lifecycle.conftest import AS_OF


def _observe(signal, state, *, backward=0):
    return CandidateObservation.from_scoring(signal, state=state, backward_transitions=backward)


def _walk_to_active(connection, lineage, signal, security_state=MarketState.CONSOLIDATION):
    """Drive one candidate DETECTION -> QUALIFICATION -> ACTIVE."""
    report = advance_lifecycle(
        connection, [_observe(signal, security_state)], as_of=AS_OF, lineage=lineage
    )
    setup_id = report.results[0].setup_id
    advance_lifecycle(
        connection,
        [_observe(signal, security_state)],
        as_of=AS_OF + timedelta(days=1),
        lineage=lineage,
    )
    advance_lifecycle(
        connection,
        [_observe(signal, MarketState.BREAKOUT_READY)],
        as_of=AS_OF + timedelta(days=2),
        lineage=lineage,
    )
    return setup_id


# --------------------------------------------------------------------------
# Derivation against real rows
# --------------------------------------------------------------------------


def test_the_setups_table_still_has_no_status_column():
    """Module 03's schema comment says adding one would reintroduce the
    mutable field the event sourcing exists to avoid. This module is the
    one with a motive to add it, so the assertion lives here."""
    assert "status" not in setups.c
    assert not [column.name for column in setups.c if "status" in column.name]


def test_status_is_derived_correctly_across_a_multi_transition_history(
    connection, register, lineage, scored
):
    """The scenario the brief asks for: several transitions, including one
    where a naive "latest row" read gives the wrong answer.

    The retreat and the endpoint that follows it share an `occurred_at`,
    because a scan observes both at once. Ordering by time leaves the
    answer to whichever row the database returns first; ordering by
    sequence does not. Here the naive read says ACTIVE and the correct one
    says OUTCOME.
    """
    security_id = register("DERIVE")
    signal = scored(security_id)
    setup_id = _walk_to_active(connection, lineage, signal)

    same_instant = AS_OF + timedelta(days=3)
    append_event(
        connection,
        setup_id,
        lifecycle_status=SetupLifecycleStatus.ACTIVE,
        event_type=RETREAT,
        occurred_at=same_instant,
        payload={"backward_transitions": 1},
    )
    append_event(
        connection,
        setup_id,
        lifecycle_status=SetupLifecycleStatus.OUTCOME,
        event_type=ENDPOINT_REACHED,
        occurred_at=same_instant,
        payload={},
    )

    assert current_status(connection, setup_id).status is SetupLifecycleStatus.OUTCOME

    # The naive query, run for real: order by time, take the first row.
    naive = connection.execute(
        select(setup_events.c.lifecycle_status)
        .where(setup_events.c.setup_id == setup_id)
        .order_by(setup_events.c.occurred_at.desc())
        .limit(1)
    ).scalar_one()
    correct = connection.execute(
        select(setup_events.c.lifecycle_status)
        .where(setup_events.c.setup_id == setup_id)
        .order_by(setup_events.c.sequence_number.desc())
        .limit(1)
    ).scalar_one()

    assert correct == SetupLifecycleStatus.OUTCOME.value
    # Not asserted equal to ACTIVE: the point is that it is unspecified.
    # Whichever value it returns, it was not chosen by anything.
    assert naive in {SetupLifecycleStatus.ACTIVE.value, SetupLifecycleStatus.OUTCOME.value}


def test_sequence_numbers_are_contiguous_from_zero(connection, register, lineage, scored):
    security_id = register("SEQ")
    setup_id = _walk_to_active(connection, lineage, scored(security_id))

    sequences = [event.sequence_number for event in history(connection, setup_id)]

    assert sequences == list(range(len(sequences)))


def test_two_events_cannot_claim_the_same_position(connection, register, lineage):
    """Module 03's `uq_setup_event_sequence`. Sequence allocation is a
    read-then-insert, so under concurrency the second writer must fail
    loudly rather than silently producing two events at one position."""
    from psycopg import errors
    from sqlalchemy.exc import IntegrityError

    setup_id, _ = open_setup(connection, register("CLASH"), as_of=AS_OF, lineage=lineage)

    savepoint = connection.begin_nested()
    with pytest.raises(IntegrityError) as raised:
        connection.execute(
            setup_events.insert().values(
                setup_id=setup_id,
                sequence_number=0,
                lifecycle_status=SetupLifecycleStatus.DETECTION.value,
                event_type="duplicate",
                occurred_at=AS_OF,
                payload={},
            )
        )
    assert isinstance(raised.value.orig, errors.UniqueViolation)
    savepoint.rollback()


def test_a_recorded_event_cannot_be_edited_or_deleted(connection, register, lineage):
    """Append-only is what makes "quietly rewrite a setup's history so a
    failure looks like something else" impossible rather than discouraged."""
    from psycopg import errors
    from sqlalchemy.exc import IntegrityError

    setup_id, event = open_setup(connection, register("FROZEN"), as_of=AS_OF, lineage=lineage)

    for statement in (
        setup_events.update().where(setup_events.c.id == event.id).values(event_type="rewritten"),
        setup_events.delete().where(setup_events.c.id == event.id),
    ):
        savepoint = connection.begin_nested()
        with pytest.raises(IntegrityError) as raised:
            connection.execute(statement)
        assert isinstance(raised.value.orig, errors.RestrictViolation)
        savepoint.rollback()

    assert current_status(connection, setup_id).events_observed == 1


def test_a_setup_cannot_be_deleted(connection, register, lineage):
    """A failed setup must be impossible to erase — Module 03 guards
    `setups` against DELETE while leaving UPDATE open for Module 15's
    review fields."""
    from psycopg import errors
    from sqlalchemy.exc import IntegrityError

    setup_id, _ = open_setup(connection, register("KEEP"), as_of=AS_OF, lineage=lineage)

    savepoint = connection.begin_nested()
    with pytest.raises(IntegrityError) as raised:
        connection.execute(setups.delete().where(setups.c.id == setup_id))
    assert isinstance(raised.value.orig, errors.RestrictViolation)
    savepoint.rollback()


# --------------------------------------------------------------------------
# Dual mode
# --------------------------------------------------------------------------


def test_a_replay_sees_only_what_had_happened_by_its_as_of(connection, register, lineage, scored):
    """The same call answers for today or for a past date, per
    CROSS_CUTTING_REQUIREMENTS.md. A backtest asking what this setup was
    on day one must not see day three's activation."""
    security_id = register("REPLAY")
    setup_id = _walk_to_active(connection, lineage, scored(security_id))

    day_zero = current_status(connection, setup_id, as_of=AS_OF)
    day_one = current_status(connection, setup_id, as_of=AS_OF + timedelta(days=1))
    today = current_status(connection, setup_id)

    assert day_zero.status is SetupLifecycleStatus.DETECTION
    assert day_one.status is SetupLifecycleStatus.QUALIFICATION
    assert today.status is SetupLifecycleStatus.ACTIVE
    assert day_zero.events_observed == 1


def test_a_replay_before_detection_has_no_status_rather_than_a_default(
    connection, register, lineage, scored
):
    """None, not DETECTION. The setup had not been observed yet, which is
    a different fact from having been detected."""
    security_id = register("BEFORE")
    setup_id = _walk_to_active(connection, lineage, scored(security_id))

    assert current_status(connection, setup_id, as_of=AS_OF - timedelta(days=1)) is None


def test_open_setups_excludes_the_concluded_ones(connection, register, lineage, scored):
    """One query for a whole scan's tracked setups, matching the batching
    discipline the earlier modules follow."""
    alive = register("ALIVE")
    done = register("DONE")
    open_id = _walk_to_active(connection, lineage, scored(alive))
    closed_id = _walk_to_active(connection, lineage, scored(done))
    append_event(
        connection,
        closed_id,
        lifecycle_status=SetupLifecycleStatus.OUTCOME,
        event_type=ENDPOINT_REACHED,
        occurred_at=AS_OF + timedelta(days=3),
        payload={},
    )

    tracked = open_setups(connection)

    assert open_id in tracked
    assert closed_id not in tracked


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


def test_reaching_uptrend_ends_the_setup_and_hands_off(connection, register, lineage, scored):
    """This module recognises that an endpoint is due; Module 15 decides
    what actually happened."""
    security_id = register("RESOLVED")
    signal = scored(security_id)
    setup_id = _walk_to_active(connection, lineage, signal)

    report = advance_lifecycle(
        connection,
        [_observe(signal, MarketState.UPTREND)],
        as_of=AS_OF + timedelta(days=5),
        lineage=lineage,
    )

    assert report.results[0].action == ENDPOINT
    event = current_status(connection, setup_id).latest_event
    assert event.event_type == ENDPOINT_REACHED
    assert event.payload["handoff"] == "Module 15 (outcome tracking)"
    assert event.payload["status_before"] == SetupLifecycleStatus.ACTIVE.value
    assert event.payload["tracked_for_days"] == pytest.approx(5.0)


def test_unclassified_never_ends_a_setup(connection, register, lineage, scored):
    """Module 10 established that UNCLASSIFIED is a change in what is
    knowable, not a retreat. A feature vector missing for one scan must
    not close setups that are perfectly alive — and closing one is
    irreversible."""
    security_id = register("UNKNOWN")
    signal = scored(security_id)
    setup_id = _walk_to_active(connection, lineage, signal)

    report = advance_lifecycle(
        connection,
        [_observe(signal, MarketState.UNCLASSIFIED)],
        as_of=AS_OF + timedelta(days=4),
        lineage=lineage,
    )

    assert report.results[0].action != ENDPOINT
    assert current_status(connection, setup_id).status is SetupLifecycleStatus.ACTIVE


def test_an_active_setup_expires_when_its_window_closes(connection, register, lineage, scored):
    security_id = register("STALE")
    signal = scored(security_id)
    setup_id = _walk_to_active(connection, lineage, signal)
    thresholds = QualificationThresholds()

    report = advance_lifecycle(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION)],
        as_of=AS_OF + thresholds.active_window + timedelta(days=3),
        lineage=lineage,
    )

    assert report.results[0].action == EXPIRED
    assert current_status(connection, setup_id).latest_event.event_type == EXPIRED_ACTIVE


def test_a_setup_that_never_qualifies_eventually_ages_out(connection, register, lineage, scored):
    """The counterweight to tracking unscoreable candidates. Without it,
    every base ARGUS ever noticed would hold its security's slot forever —
    and a new base forming later could not open a setup."""
    security_id = register("FOREVER")
    signal = scored(security_id, strong=False)
    report = advance_lifecycle(
        connection, [_observe(signal, MarketState.CONSOLIDATION)], as_of=AS_OF, lineage=lineage
    )
    setup_id = report.results[0].setup_id

    aged = advance_lifecycle(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION)],
        as_of=AS_OF + QualificationThresholds().detection_window + timedelta(days=1),
        lineage=lineage,
    )

    assert aged.results[0].action == EXPIRED
    assert current_status(connection, setup_id).latest_event.event_type == EXPIRED_UNQUALIFIED


def test_a_new_setup_opens_once_the_old_one_has_concluded(connection, register, lineage, scored):
    """A security gets one open setup at a time, and a later base is a new
    setup rather than a resurrection of the closed one."""
    security_id = register("AGAIN")
    signal = scored(security_id, strong=False)
    first = advance_lifecycle(
        connection, [_observe(signal, MarketState.CONSOLIDATION)], as_of=AS_OF, lineage=lineage
    )
    append_event(
        connection,
        first.results[0].setup_id,
        lifecycle_status=SetupLifecycleStatus.OUTCOME,
        event_type=ENDPOINT_REACHED,
        occurred_at=AS_OF + timedelta(days=1),
        payload={},
    )

    second = advance_lifecycle(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION)],
        as_of=AS_OF + timedelta(days=2),
        lineage=lineage,
    )

    assert second.results[0].action == OPENED
    assert second.results[0].setup_id != first.results[0].setup_id
    assert len(connection.execute(select(setups)).all()) == 2


def test_the_expiry_windows_are_read_from_the_configuration(connection, register, lineage, scored):
    """Move the threshold and the verdict must move, or the window is not
    really configurable."""
    security_id = register("SHORT")
    signal = scored(security_id, strong=False)
    advance_lifecycle(
        connection, [_observe(signal, MarketState.CONSOLIDATION)], as_of=AS_OF, lineage=lineage
    )
    impatient = LifecycleConfig(
        thresholds=QualificationThresholds(
            max_detection_duration_days=LifecycleThreshold(
                value=2.0, kind=CALIBRATABLE, rationale="test override"
            )
        )
    )

    report = advance_lifecycle(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION)],
        as_of=AS_OF + timedelta(days=3),
        lineage=lineage,
        config=impatient,
    )

    assert report.results[0].action == EXPIRED


# --------------------------------------------------------------------------
# One open setup per security — now structural (migration 0006)
# --------------------------------------------------------------------------


def test_the_database_rejects_a_second_open_setup_for_one_security(connection, register, lineage):
    """Not just resolved gracefully by application code — refused.

    Module 14 maintained this invariant in `_index_by_security` and
    flagged that the schema did not enforce it. Two open setups would make
    "which setup does this candidate belong to" ambiguous at every later
    scan, and under concurrency the application check could be passed by
    both writers.
    """
    from psycopg import errors
    from sqlalchemy.exc import IntegrityError

    security_id = register("ONLYONE")
    open_setup(connection, security_id, as_of=AS_OF, lineage=lineage)

    savepoint = connection.begin_nested()
    with pytest.raises(IntegrityError) as raised:
        open_setup(connection, security_id, as_of=AS_OF + timedelta(days=1), lineage=lineage)
    assert isinstance(raised.value.orig, errors.UniqueViolation)
    savepoint.rollback()


def test_concluding_a_setup_frees_the_security_for_a_new_one(connection, register, lineage):
    """The index is partial on `concluded_at IS NULL`, so a finished setup
    stops occupying its security's slot — which is what makes a second
    base on the same security a new setup rather than an impossibility."""
    security_id = register("REUSE")
    first, _ = open_setup(connection, security_id, as_of=AS_OF, lineage=lineage)
    append_event(
        connection,
        first,
        lifecycle_status=SetupLifecycleStatus.OUTCOME,
        event_type=ENDPOINT_REACHED,
        occurred_at=AS_OF + timedelta(days=1),
        payload={},
    )

    second, _ = open_setup(
        connection, security_id, as_of=AS_OF + timedelta(days=2), lineage=lineage
    )

    assert second != first
    concluded = connection.execute(
        select(setups.c.concluded_at).where(setups.c.id == first)
    ).scalar_one()
    assert concluded == AS_OF + timedelta(days=1)


def test_the_terminal_marker_is_set_on_every_write_path(connection, register, lineage, scored):
    """`advance()` and a direct `append_event` must both maintain it.

    Only one of the two did when the column was first added, which left a
    concluded setup still occupying its security's slot. The marker
    belongs in the single write path, not in the convenience wrapper.
    """
    via_advance = register("VIAADV")
    setup_id = _walk_to_active(connection, lineage, scored(via_advance))
    advance_lifecycle(
        connection,
        [_observe(scored(via_advance), MarketState.UPTREND)],
        as_of=AS_OF + timedelta(days=4),
        lineage=lineage,
    )

    marker = connection.execute(
        select(setups.c.concluded_at).where(setups.c.id == setup_id)
    ).scalar_one()
    assert marker == AS_OF + timedelta(days=4)


def test_the_marker_never_disagrees_with_the_derived_status(connection, register, lineage, scored):
    """The projection rule, asserted.

    `concluded_at` is a projection of the event log, exactly as Module
    10's `market_state` is a projection of its transitions. The log stays
    authoritative — `state_from` never reads this column — so the only
    thing that can go wrong is drift, and this is what makes drift visible.
    """
    open_id = _walk_to_active(connection, lineage, scored(register("DRIFTA")))
    closed_id = _walk_to_active(connection, lineage, scored(register("DRIFTB")))
    append_event(
        connection,
        closed_id,
        lifecycle_status=SetupLifecycleStatus.OUTCOME,
        event_type=ENDPOINT_REACHED,
        occurred_at=AS_OF + timedelta(days=3),
        payload={},
    )

    for setup_id in (open_id, closed_id):
        marker = connection.execute(
            select(setups.c.concluded_at).where(setups.c.id == setup_id)
        ).scalar_one()
        derived = current_status(connection, setup_id)
        assert (marker is not None) == derived.is_terminal, setup_id


def test_the_derivation_still_ignores_the_marker(connection, register, lineage, scored):
    """Belt and braces: corrupt the projection and the derived status must
    not move. If it did, the log would have stopped being authoritative."""
    setup_id = _walk_to_active(connection, lineage, scored(register("IGNORED")))
    connection.execute(
        setups.update()
        .where(setups.c.id == setup_id)
        .values(concluded_at=AS_OF + timedelta(days=99))
    )

    assert current_status(connection, setup_id).status is SetupLifecycleStatus.ACTIVE
