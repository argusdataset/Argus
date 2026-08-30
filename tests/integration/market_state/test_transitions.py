"""Transition recording, backward moves, duration, and history queries.

The failed breakout is the case this file exists for. A security moving
from `BREAKOUT_READY` back to `CONSOLIDATION` is normal and carries
information — a name on its third attempt is a different proposition from
one that cleared the level first try — and the only place that difference
is recorded is `market_state_transitions`.

Nothing in Module 10 uses that history. Module 11 will, and it can only do
so if the history is correct and complete now, which is why
`cycle_count()` and `backward_transition_count()` are tested here before
anything consumes them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from core.market_state import (
    MarketStateConfig,
    all_watchlists,
    backward_transition_count,
    cycle_count,
    history,
    load_current_states,
    publish_target_model_version,
    record_transitions,
    watchlist,
)
from core.market_state.classifier import ClassificationResult, StateAssignment
from infra.db.enums import MarketState
from infra.db.schema.intelligence import market_state, market_state_transitions

START = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def version_id(connection: Connection) -> UUID:
    return publish_target_model_version(connection, MarketStateConfig())


def _result(
    version_id: UUID,
    as_of: datetime,
    states: dict[UUID, MarketState],
    *,
    confidence: float | None = None,
) -> ClassificationResult:
    return ClassificationResult(
        as_of=as_of,
        target_model_version_id=version_id,
        assignments={
            security_id: StateAssignment(
                security_id=security_id,
                state=state,
                confidence=confidence,
                evidence={"matched_state": state.value},
            )
            for security_id, state in states.items()
        },
    )


def _walk(
    connection: Connection,
    version_id: UUID,
    security_id: UUID,
    states: list[MarketState],
    *,
    step: timedelta = timedelta(days=30),
) -> None:
    """Drive one security through a sequence of states, one `as_of` apart."""
    for index, state in enumerate(states):
        record_transitions(
            connection, _result(version_id, START + step * index, {security_id: state})
        )


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def test_the_first_classification_records_a_transition_with_no_prior_state(
    connection: Connection, register, version_id: UUID
):
    """`from_state` is NULL and duration is NULL — the only case where both are."""
    security_id = register("FIRST")
    transitions = record_transitions(
        connection, _result(version_id, START, {security_id: MarketState.DOWN_TREND})
    )

    assert len(transitions) == 1
    assert transitions[0].from_state is None
    assert transitions[0].duration_in_prior_state is None
    assert transitions[0].to_state is MarketState.DOWN_TREND


def test_a_state_change_records_the_duration_spent_in_the_prior_state(
    connection: Connection, register, version_id: UUID
):
    """Duration is stored, not recomputed later.

    Module 03's schema requires it whenever `from_state` is present, and
    a `CHECK` constraint enforces that pairing — so getting this wrong is
    a write failure rather than a silent gap.
    """
    security_id = register("DURATION")
    record_transitions(
        connection, _result(version_id, START, {security_id: MarketState.CONSOLIDATION})
    )
    later = START + timedelta(days=45)
    transitions = record_transitions(
        connection, _result(version_id, later, {security_id: MarketState.ACCUMULATION})
    )

    assert len(transitions) == 1
    assert transitions[0].from_state is MarketState.CONSOLIDATION
    assert transitions[0].duration_in_prior_state == timedelta(days=45)


def test_reclassifying_to_the_same_state_records_nothing(
    connection: Connection, register, version_id: UUID
):
    """A transition row means "this changed", not "we looked".

    Emitting one per scan would turn the history into a sampling log, and
    `cycle_count()` would count scans rather than cycles — destroying the
    one question the table exists to answer.
    """
    security_id = register("STABLE")
    record_transitions(
        connection, _result(version_id, START, {security_id: MarketState.CONSOLIDATION})
    )
    again = record_transitions(
        connection,
        _result(version_id, START + timedelta(days=1), {security_id: MarketState.CONSOLIDATION}),
    )

    assert again == []
    assert len(history(connection, security_id)) == 1


def test_an_unstamped_result_is_refused(connection: Connection, register):
    """No version, no write — the thresholds must stay attributable.

    These thresholds in particular are placeholders that *will* change,
    so a state recorded without the version that produced it would be
    permanently uninterpretable.
    """
    security_id = register("UNSTAMPED")
    result = ClassificationResult(
        as_of=START,
        target_model_version_id=None,
        assignments={
            security_id: StateAssignment(
                security_id=security_id,
                state=MarketState.DOWN_TREND,
                confidence=None,
                evidence={},
            )
        },
    )

    with pytest.raises(ValueError, match="target_model_version_id"):
        record_transitions(connection, result)


# --------------------------------------------------------------------------
# Backward transitions — the failed breakout
# --------------------------------------------------------------------------


def test_a_failed_breakout_is_recorded_not_treated_as_an_error(
    connection: Connection, register, version_id: UUID
):
    """BREAKOUT_READY back to CONSOLIDATION. Normal, expected, recorded."""
    security_id = register("FAILED")
    _walk(
        connection,
        version_id,
        security_id,
        [MarketState.CONSOLIDATION, MarketState.BREAKOUT_READY, MarketState.CONSOLIDATION],
    )

    rows = history(connection, security_id)
    assert [t.to_state for t in rows] == [
        MarketState.CONSOLIDATION,
        MarketState.BREAKOUT_READY,
        MarketState.CONSOLIDATION,
    ]
    assert rows[-1].is_backward
    assert not rows[1].is_backward


def test_the_backward_flag_is_stored_rather_than_derived_on_read(
    connection: Connection, register, version_id: UUID
):
    """So a future reordering of CYCLE_ORDER cannot reinterpret history.

    If direction were computed at read time, redefining the cycle would
    silently change what every historical row meant — the same hazard
    Module 05's restatement rule exists to prevent for prices.
    """
    security_id = register("FLAGGED")
    _walk(
        connection,
        version_id,
        security_id,
        [MarketState.BREAKOUT_WATCH, MarketState.CONSOLIDATION],
    )

    stored = (
        connection.execute(
            select(market_state_transitions.c.evidence)
            .where(market_state_transitions.c.security_id == security_id)
            .order_by(market_state_transitions.c.transition_time)
        )
        .scalars()
        .all()
    )

    assert stored[-1]["backward_transition"] is True
    assert backward_transition_count(connection, security_id) == 1


def test_moving_to_unclassified_is_neither_forward_nor_backward(
    connection: Connection, register, version_id: UUID
):
    """UNCLASSIFIED sits outside the cycle.

    A security losing eligibility is a change in what is *knowable*, not a
    retreat along the setup, and counting it as a failed breakout would
    corrupt exactly the statistic Module 11 wants.
    """
    security_id = register("LOSTELIG")
    _walk(
        connection,
        version_id,
        security_id,
        [MarketState.BREAKOUT_READY, MarketState.UNCLASSIFIED],
    )

    assert backward_transition_count(connection, security_id) == 0


# --------------------------------------------------------------------------
# History queries — built for Module 11
# --------------------------------------------------------------------------


def test_cycle_count_answers_how_many_times_a_state_was_entered(
    connection: Connection, register, version_id: UUID
):
    """Three attempts at a breakout versus one — the Module 11 question."""
    persistent = register("THIRDTIME")
    _walk(
        connection,
        version_id,
        persistent,
        [
            MarketState.CONSOLIDATION,
            MarketState.BREAKOUT_WATCH,
            MarketState.CONSOLIDATION,
            MarketState.BREAKOUT_WATCH,
            MarketState.CONSOLIDATION,
            MarketState.BREAKOUT_WATCH,
            MarketState.UPTREND,
        ],
    )
    first_try = register("FIRSTTIME")
    _walk(
        connection,
        version_id,
        first_try,
        [MarketState.CONSOLIDATION, MarketState.BREAKOUT_WATCH, MarketState.UPTREND],
    )

    assert cycle_count(connection, persistent, MarketState.BREAKOUT_WATCH) == 3
    assert cycle_count(connection, first_try, MarketState.BREAKOUT_WATCH) == 1
    assert backward_transition_count(connection, persistent) == 2
    assert backward_transition_count(connection, first_try) == 0


def test_history_is_bounded_by_as_of(connection: Connection, register, version_id: UUID):
    """A replay asking "how many attempts by 2015" must not see 2016's.

    `as_of` is a plain argument here for the same reason it is everywhere
    else in ARGUS — and without this bound, transition history would be
    the one place a backtest could see its own future.
    """
    security_id = register("BOUNDED")
    _walk(
        connection,
        version_id,
        security_id,
        [
            MarketState.CONSOLIDATION,
            MarketState.BREAKOUT_WATCH,
            MarketState.CONSOLIDATION,
            MarketState.BREAKOUT_WATCH,
        ],
    )

    midpoint = START + timedelta(days=45)
    assert cycle_count(connection, security_id, MarketState.BREAKOUT_WATCH) == 2
    assert cycle_count(connection, security_id, MarketState.BREAKOUT_WATCH, as_of=midpoint) == 1
    assert len(history(connection, security_id, as_of=midpoint)) == 2


def test_history_is_ordered_oldest_first(connection: Connection, register, version_id: UUID):
    security_id = register("ORDERED")
    _walk(
        connection,
        version_id,
        security_id,
        [MarketState.DOWN_TREND, MarketState.BASE_FORMING, MarketState.CONSOLIDATION],
    )

    times = [t.transition_time for t in history(connection, security_id)]
    assert times == sorted(times)


# --------------------------------------------------------------------------
# The projection
# --------------------------------------------------------------------------


def test_the_projection_holds_one_row_per_security(
    connection: Connection, register, version_id: UUID
):
    """`market_state` is current state, not history. The log is the authority."""
    security_id = register("PROJECTED")
    _walk(
        connection,
        version_id,
        security_id,
        [MarketState.DOWN_TREND, MarketState.BASE_FORMING, MarketState.CONSOLIDATION],
    )

    rows = connection.execute(
        select(func.count())
        .select_from(market_state)
        .where(market_state.c.security_id == security_id)
    ).scalar_one()
    assert rows == 1

    current = load_current_states(connection, [security_id])[security_id]
    assert current.state is MarketState.CONSOLIDATION
    assert len(history(connection, security_id)) == 3


def test_the_projection_can_be_rebuilt_from_the_log(
    connection: Connection, register, version_id: UUID
):
    """The direction of authority, asserted.

    The log determines the projection; the projection cannot determine the
    log. When they disagree, the log is right — so the last transition
    must always match the projection.
    """
    security_id = register("REBUILD")
    _walk(
        connection,
        version_id,
        security_id,
        [MarketState.CONSOLIDATION, MarketState.BREAKOUT_WATCH, MarketState.CONSOLIDATION],
    )

    last = history(connection, security_id)[-1]
    current = load_current_states(connection, [security_id])[security_id]

    assert current.state is last.to_state
    assert current.entered_at == last.transition_time


# --------------------------------------------------------------------------
# Watchlists as queries over the projection
# --------------------------------------------------------------------------


def test_the_watchlists_are_queries_over_market_state(
    connection: Connection, register, version_id: UUID
):
    """No stored watchlist table anywhere — the Source-of-Truth principle."""
    down = register("WLDOWN")
    consolidating = register("WLCONS")
    ready = register("WLREADY")
    broken_out = register("WLUP")
    topping = register("WLDIST")

    record_transitions(
        connection,
        _result(
            version_id,
            START,
            {
                down: MarketState.BASE_FORMING,
                consolidating: MarketState.ACCUMULATION,
                ready: MarketState.BREAKOUT_WATCH,
                broken_out: MarketState.UPTREND,
                topping: MarketState.DISTRIBUTION,
            },
        ),
    )

    lists = all_watchlists(connection)
    assert down in lists["DOWN_TREND"]
    assert consolidating in lists["CONSOLIDATION"]
    assert ready in lists["BREAKOUT_READY"]
    # UPTREND is the fourth watchlist, a confirmed move ARGUS wants shown.
    assert broken_out in lists["UPTREND"]
    # DISTRIBUTION is internal — it appears on none of the four.
    assert not any(topping in members for members in lists.values())


def test_an_unclassified_security_appears_on_no_watchlist(
    connection: Connection, register, version_id: UUID
):
    """The whole point of having UNCLASSIFIED.

    A security that is ineligible or too new must be absent, not defaulted
    into a list — otherwise every watchlist quietly accumulates securities
    nobody ever judged.
    """
    security_id = register("WLUNCLS")
    record_transitions(
        connection, _result(version_id, START, {security_id: MarketState.UNCLASSIFIED})
    )

    for members in all_watchlists(connection).values():
        assert security_id not in members


def test_moving_between_states_moves_the_security_between_watchlists(
    connection: Connection, register, version_id: UUID
):
    """Derived, so membership follows state with no separate update."""
    security_id = register("WLMOVER")

    record_transitions(
        connection, _result(version_id, START, {security_id: MarketState.CONSOLIDATION})
    )
    assert security_id in watchlist(connection, "CONSOLIDATION")

    record_transitions(
        connection,
        _result(version_id, START + timedelta(days=30), {security_id: MarketState.BREAKOUT_READY}),
    )
    assert security_id not in watchlist(connection, "CONSOLIDATION")
    assert security_id in watchlist(connection, "BREAKOUT_READY")


def test_recording_a_batch_issues_a_constant_number_of_queries(
    connection: Connection, register, version_id: UUID
):
    """Batched, not looped — the same property Modules 08 and 09 assert."""
    from sqlalchemy import event

    class Counter:
        def __init__(self) -> None:
            self.count = 0

        def __call__(self, *_a: object, **_k: object) -> None:
            self.count += 1

    securities = [register(f"BATCH{i:02d}") for i in range(20)]

    counter = Counter()
    event.listen(connection, "before_cursor_execute", counter)
    try:
        record_transitions(
            connection,
            _result(version_id, START, dict.fromkeys(securities, MarketState.CONSOLIDATION)),
        )
    finally:
        event.remove(connection, "before_cursor_execute", counter)

    # One read of the projection, one transition insert, one projection upsert.
    assert counter.count == 3, f"expected 3 queries for 20 securities, got {counter.count}"


def test_the_query_count_does_not_grow_with_the_batch(
    connection: Connection, register, version_id: UUID
):
    """Constant, not merely small."""
    from sqlalchemy import event

    def count_for(n: int) -> int:
        securities = [register(f"SCALE{n}_{i:02d}") for i in range(n)]
        seen = {"count": 0}

        def listener(*_a: object, **_k: object) -> None:
            seen["count"] += 1

        event.listen(connection, "before_cursor_execute", listener)
        try:
            record_transitions(
                connection,
                _result(
                    version_id,
                    START,
                    dict.fromkeys(securities, MarketState.DOWN_TREND),
                ),
            )
        finally:
            event.remove(connection, "before_cursor_execute", listener)
        return seen["count"]

    assert count_for(3) == count_for(40) == 3


def test_publishing_the_same_configuration_twice_returns_one_version(
    connection: Connection,
):
    """Idempotent by checksum, as Modules 08 and 09 established."""
    first = publish_target_model_version(connection, MarketStateConfig())
    second = publish_target_model_version(connection, MarketStateConfig())
    assert first == second


def test_the_stored_version_says_the_thresholds_are_unvalidated(
    connection: Connection, version_id: UUID
):
    """Recorded in the database, not only in a docstring.

    Anyone reading `target_model_version` later — including a future
    calibration effort — sees immediately that these numbers were never
    measured.
    """
    from infra.db.schema.versioning import target_model_version

    definition = connection.execute(
        select(target_model_version.c.definition).where(target_model_version.c.id == version_id)
    ).scalar_one()

    assert definition["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"
    assert definition["state_thresholds"]
    assert definition["target_model_thresholds"]
