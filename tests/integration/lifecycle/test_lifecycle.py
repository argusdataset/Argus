"""The lifecycle against a real database: progression, gates, derivation."""

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
from core.lifecycle.derivation import (
    LifecycleRegression,
    advance,
    current_status,
    history,
)
from core.lifecycle.engine import (
    ACTIVATED_ACTION,
    GATED_NO_SETUP,
    INVALIDATED,
    NOT_TRACKED,
    OPENED,
    QUALIFIED_ACTION,
    RETREAT_RECORDED,
    UNCHANGED,
    CandidateObservation,
    advance_lifecycle,
)
from core.lifecycle.events import (
    DETECTED,
    INVALIDATED_LOST_ELIGIBILITY,
    QUALIFIED,
    RETREAT,
)
from core.scoring.gating import ScoringDecision
from infra.db.enums import MarketState, SetupLifecycleStatus
from infra.db.schema.setups import setup_events, setups
from tests.integration.lifecycle.conftest import AS_OF


def _observe(signal, state, *, backward=0):
    return CandidateObservation.from_scoring(signal, state=state, backward_transitions=backward)


def _run(connection, observations, lineage, *, as_of=AS_OF, config=None):
    return advance_lifecycle(connection, observations, as_of=as_of, lineage=lineage, config=config)


# --------------------------------------------------------------------------
# Full progression
# --------------------------------------------------------------------------


def test_a_qualifying_candidate_walks_detection_to_active(connection, register, lineage, scored):
    """One transition per scan, driven by real Module 13 output.

    The intermediate QUALIFICATION event is not a formality: it is the
    record that the bar was cleared, on that date, under that
    configuration. A history that jumped straight to ACTIVE could not be
    audited afterwards.
    """
    security_id = register("WALK")
    signal = scored(security_id)
    assert signal.decision is ScoringDecision.SCORED

    first = _run(connection, [_observe(signal, MarketState.CONSOLIDATION)], lineage)
    assert first.results[0].action == OPENED

    setup_id = first.results[0].setup_id
    assert current_status(connection, setup_id).status is SetupLifecycleStatus.DETECTION

    second = _run(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION)],
        lineage,
        as_of=AS_OF + timedelta(days=1),
    )
    assert second.results[0].action == QUALIFIED_ACTION
    assert current_status(connection, setup_id).status is SetupLifecycleStatus.QUALIFICATION

    third = _run(
        connection,
        [_observe(signal, MarketState.BREAKOUT_WATCH)],
        lineage,
        as_of=AS_OF + timedelta(days=2),
    )
    assert third.results[0].action == ACTIVATED_ACTION

    state = current_status(connection, setup_id)
    assert state.status is SetupLifecycleStatus.ACTIVE
    assert state.events_observed == 3
    assert [event.event_type for event in history(connection, setup_id)] == [
        DETECTED,
        QUALIFIED,
        "activated",
    ]


def test_the_qualification_event_records_the_bar_it_cleared(connection, register, lineage, scored):
    """A setup qualified under today's placeholder bar must stay
    attributable to it after the bar moves."""
    security_id = register("BAR")
    signal = scored(security_id)
    report = _run(connection, [_observe(signal, MarketState.CONSOLIDATION)], lineage)
    setup_id = report.results[0].setup_id
    _run(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION)],
        lineage,
        as_of=AS_OF + timedelta(days=1),
    )

    qualification = history(connection, setup_id)[-1]

    assert qualification.event_type == QUALIFIED
    assert qualification.payload["argus_score"] == pytest.approx(signal.argus_score)
    assert qualification.payload["thresholds"] == QualificationThresholds().as_dict()
    assert qualification.payload["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"


def test_a_setup_below_the_bar_stays_detected_rather_than_being_closed(
    connection, register, lineage, scored
):
    """Not good enough is not the same as over."""
    security_id = register("BELOW")
    signal = scored(security_id)
    strict = LifecycleConfig(
        thresholds=QualificationThresholds(
            min_argus_score=LifecycleThreshold(
                value=99.0, kind=CALIBRATABLE, rationale="test override"
            )
        )
    )

    _run(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION)],
        lineage,
        config=strict,
    )
    second = _run(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION)],
        lineage,
        as_of=AS_OF + timedelta(days=1),
        config=strict,
    )

    assert second.results[0].action == UNCHANGED
    assert second.results[0].status is SetupLifecycleStatus.DETECTION
    assert "the bar is 99.0" in second.results[0].reason


# --------------------------------------------------------------------------
# The DETECTION boundary
# --------------------------------------------------------------------------


def test_an_unscoreable_candidate_is_still_tracked_at_detection(
    connection, register, lineage, scored
):
    """The module's central decision, asserted.

    Requiring a score to open a setup would deadlock ARGUS: no setups, no
    outcomes, no historical cases, so Module 11 stays empty, so Module 13
    keeps refusing to score, so no setups. Detection is driven by Module
    10's state instead, and today every setup sits here — which is the
    honest picture rather than a failure.
    """
    security_id = register("NOSCORE")
    signal = scored(security_id, strong=False)
    assert signal.decision is ScoringDecision.INSUFFICIENT_EVIDENCE

    report = _run(connection, [_observe(signal, MarketState.CONSOLIDATION)], lineage)

    assert report.results[0].action == OPENED
    setup_id = report.results[0].setup_id
    assert current_status(connection, setup_id).status is SetupLifecycleStatus.DETECTION


def test_an_unscoreable_candidate_never_qualifies(connection, register, lineage, scored):
    """Tracked, but not committed to. The two are different and the
    lifecycle keeps them apart."""
    security_id = register("NOQUAL")
    signal = scored(security_id, strong=False)
    _run(connection, [_observe(signal, MarketState.CONSOLIDATION)], lineage)

    second = _run(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION)],
        lineage,
        as_of=AS_OF + timedelta(days=1),
    )

    assert second.results[0].action == UNCHANGED
    assert second.results[0].status is SetupLifecycleStatus.DETECTION
    assert "Module 17" in second.results[0].reason


def test_a_security_outside_the_pattern_states_is_not_tracked(
    connection, register, lineage, scored
):
    """Detection is not "everything ARGUS looked at"."""
    security_id = register("OUTSIDE")
    report = _run(
        connection,
        [_observe(scored(security_id), MarketState.DOWN_TREND)],
        lineage,
    )

    assert report.results[0].action == NOT_TRACKED
    assert report.results[0].setup_id is None
    assert connection.execute(select(setups)).all() == []


# --------------------------------------------------------------------------
# Gated candidates
# --------------------------------------------------------------------------


def test_lost_eligibility_on_an_active_setup_becomes_a_setup_event(
    connection, register, lineage, scored
):
    """Module 13 writes no signal row for a gated candidate, so this is
    the only durable record that the thesis broke — which is exactly what
    Module 13's report handed to this module."""
    security_id = register("BROKEN")
    healthy = scored(security_id)

    report = _run(connection, [_observe(healthy, MarketState.CONSOLIDATION)], lineage)
    setup_id = report.results[0].setup_id
    _run(
        connection,
        [_observe(healthy, MarketState.CONSOLIDATION)],
        lineage,
        as_of=AS_OF + timedelta(days=1),
    )
    _run(
        connection,
        [_observe(healthy, MarketState.BREAKOUT_READY)],
        lineage,
        as_of=AS_OF + timedelta(days=2),
    )
    assert current_status(connection, setup_id).status is SetupLifecycleStatus.ACTIVE

    broken = scored(security_id, gated=True)
    assert broken.decision is ScoringDecision.GATED_LOST_ELIGIBILITY

    final = _run(
        connection,
        [_observe(broken, MarketState.CONSOLIDATION, backward=1)],
        lineage,
        as_of=AS_OF + timedelta(days=3),
    )

    assert final.results[0].action == INVALIDATED
    state = current_status(connection, setup_id)
    assert state.status is SetupLifecycleStatus.OUTCOME
    assert state.latest_event.event_type == INVALIDATED_LOST_ELIGIBILITY


def test_the_invalidation_event_carries_module_13s_verdict_detail(
    connection, register, lineage, scored
):
    """The eligibility trend, when the security last passed, and which
    gates newly failed — carried whole rather than summarised, because
    re-deriving them later would mean re-running Module 09 against data
    that has moved on."""
    security_id = register("DETAIL")
    report = _run(
        connection,
        [_observe(scored(security_id), MarketState.CONSOLIDATION)],
        lineage,
    )
    setup_id = report.results[0].setup_id

    broken = scored(security_id, gated=True)
    _run(
        connection,
        [_observe(broken, MarketState.CONSOLIDATION, backward=2)],
        lineage,
        as_of=AS_OF + timedelta(days=1),
    )

    event = history(connection, setup_id)[-1]
    payload = event.payload

    assert payload["decision"] == ScoringDecision.GATED_LOST_ELIGIBILITY.value
    assert payload["verdict_detail"]["trend"] == "lost_eligibility"
    assert payload["verdict_detail"]["routed_to"] == "Module 14 (lifecycle)"
    assert payload["backward_transitions"] == 2
    assert payload["status_before"] == SetupLifecycleStatus.DETECTION.value
    assert payload["handoff"] == "Module 15 (outcome tracking)"
    assert payload["tracked_for_days"] == pytest.approx(1.0)


def test_a_gated_candidate_with_no_setup_writes_nothing(connection, register, lineage, scored):
    """Deliberate. Creating a setup to record that it should not exist
    would turn `setups` into a log of everything ARGUS ever looked at,
    once per scan across a ten-thousand-name universe. Module 09's
    eligibility_check_results already records the rejection durably."""
    security_id = register("NEVER")
    broken = scored(security_id, gated=True)

    report = _run(connection, [_observe(broken, MarketState.CONSOLIDATION)], lineage)

    assert report.results[0].action == GATED_NO_SETUP
    assert report.results[0].setup_id is None
    assert connection.execute(select(setups)).all() == []
    assert connection.execute(select(setup_events)).all() == []


# --------------------------------------------------------------------------
# Backward movement
# --------------------------------------------------------------------------


def test_a_retreat_on_an_active_setup_is_a_normal_recorded_transition(
    connection, register, lineage, scored
):
    """Not an error, and not a demotion.

    Module 10 established that backward movement is first-class. Here it
    lands as an event *at* ACTIVE carrying Module 10's own count, so the
    history holds it while the derived status keeps answering "how far has
    ARGUS committed".
    """
    security_id = register("RETREAT")
    signal = scored(security_id)

    report = _run(connection, [_observe(signal, MarketState.CONSOLIDATION, backward=0)], lineage)
    setup_id = report.results[0].setup_id
    _run(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION, backward=0)],
        lineage,
        as_of=AS_OF + timedelta(days=1),
    )
    _run(
        connection,
        [_observe(signal, MarketState.BREAKOUT_READY, backward=0)],
        lineage,
        as_of=AS_OF + timedelta(days=2),
    )

    retreated = _run(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION, backward=1)],
        lineage,
        as_of=AS_OF + timedelta(days=3),
    )

    assert retreated.results[0].action == RETREAT_RECORDED
    state = current_status(connection, setup_id)
    assert state.status is SetupLifecycleStatus.ACTIVE
    assert state.latest_event.event_type == RETREAT
    assert state.latest_event.payload["backward_transitions_before"] == 0
    assert state.latest_event.payload["backward_transitions"] == 1


def test_the_lifecycle_refuses_to_run_backwards(connection, register, lineage, scored):
    """The log would still hold everything, but the derived status would
    start answering a different question — and every consumer reads the
    derived status."""
    security_id = register("NOBACK")
    signal = scored(security_id)
    report = _run(connection, [_observe(signal, MarketState.CONSOLIDATION)], lineage)
    setup_id = report.results[0].setup_id
    _run(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION)],
        lineage,
        as_of=AS_OF + timedelta(days=1),
    )

    with pytest.raises(LifecycleRegression):
        advance(
            connection,
            setup_id,
            to=SetupLifecycleStatus.DETECTION,
            event_type="rewind",
            occurred_at=AS_OF + timedelta(days=2),
        )


def test_a_retreat_without_module_10s_count_is_not_guessed_at(
    connection, register, lineage, scored
):
    """None means "not read", and the comparison is skipped rather than
    treated as zero — which would report a retreat on the first scan of
    every active setup."""
    security_id = register("NOCOUNT")
    signal = scored(security_id)
    report = _run(connection, [_observe(signal, MarketState.CONSOLIDATION)], lineage)
    setup_id = report.results[0].setup_id
    _run(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION)],
        lineage,
        as_of=AS_OF + timedelta(days=1),
    )
    _run(
        connection,
        [_observe(signal, MarketState.BREAKOUT_READY)],
        lineage,
        as_of=AS_OF + timedelta(days=2),
    )

    blind = advance_lifecycle(
        connection,
        [CandidateObservation.from_scoring(signal, state=MarketState.CONSOLIDATION)],
        as_of=AS_OF + timedelta(days=3),
        lineage=lineage,
    )

    assert blind.results[0].action == UNCHANGED
    assert "was not supplied" in blind.results[0].reason
    assert current_status(connection, setup_id).events_observed == 3


def test_a_first_available_count_is_not_read_as_a_fresh_retreat(
    connection, register, lineage, scored
):
    """The other direction of the same care, and the likelier one.

    A setup can be opened before Module 12 starts supplying Module 10's
    backward count — the observation simply did not carry it. When a count
    then appears, it is a *total* over the security's whole history, not a
    change since the last scan. Treating the absent previous value as zero
    would turn every old retreat into a new event, dated now, on the first
    scan that could see it.
    """
    security_id = register("FIRSTCOUNT")
    signal = scored(security_id)

    blind = CandidateObservation.from_scoring(signal, state=MarketState.CONSOLIDATION)
    report = advance_lifecycle(connection, [blind], as_of=AS_OF, lineage=lineage)
    setup_id = report.results[0].setup_id
    advance_lifecycle(connection, [blind], as_of=AS_OF + timedelta(days=1), lineage=lineage)
    advance_lifecycle(
        connection,
        [CandidateObservation.from_scoring(signal, state=MarketState.BREAKOUT_READY)],
        as_of=AS_OF + timedelta(days=2),
        lineage=lineage,
    )
    assert current_status(connection, setup_id).latest_event.payload["backward_transitions"] is None

    # Now the count arrives, carrying three retreats from before ARGUS was
    # looking at this setup.
    informed = _run(
        connection,
        [_observe(signal, MarketState.CONSOLIDATION, backward=3)],
        lineage,
        as_of=AS_OF + timedelta(days=3),
    )

    assert informed.results[0].action == UNCHANGED
    assert current_status(connection, setup_id).events_observed == 3
