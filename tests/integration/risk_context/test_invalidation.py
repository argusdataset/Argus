"""Invalidation signals, read from what Modules 09 and 10 actually wrote.

These deliberately go through Module 10's `record_transitions` and write
`eligibility_check_results` in the shape Module 09's persistence produces,
rather than hand-building the rows this module wants to read. The coupling
between the modules is itself something that can break — a renamed
column, an evidence key that stops being written — and a hand-built
fixture would never notice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection

from core.market_state import (
    MarketStateConfig,
    publish_target_model_version,
    record_transitions,
)
from core.market_state.classifier import ClassificationResult, StateAssignment
from core.risk_context.invalidation import (
    BACKWARD_TRANSITION,
    LOST_ELIGIBILITY,
    EligibilityTrend,
    assess_eligibility_change,
    assess_invalidation,
    eligibility_history,
)
from infra.db.enums import EligibilityGate, MarketState

START = datetime(2024, 1, 1, tzinfo=UTC)
LATER = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def version_id(connection: Connection) -> UUID:
    return publish_target_model_version(connection, MarketStateConfig())


def _walk(
    connection: Connection,
    version_id: UUID,
    security_id: UUID,
    states: list[MarketState],
    *,
    start: datetime = START,
) -> None:
    """Move one security through a sequence of states, a week apart."""
    for step, state in enumerate(states):
        record_transitions(
            connection,
            ClassificationResult(
                as_of=start + timedelta(weeks=step),
                target_model_version_id=version_id,
                assignments={
                    security_id: StateAssignment(
                        security_id=security_id,
                        state=state,
                        confidence=None,
                        evidence={"matched_state": state.value},
                    )
                },
            ),
        )


# --------------------------------------------------------------------------
# Backward transitions
# --------------------------------------------------------------------------


def test_a_failed_breakout_surfaces_as_an_invalidation_signal(connection, register, version_id):
    """BREAKOUT_READY back to CONSOLIDATION: the canonical retreat."""
    security_id = register("FAILED")
    _walk(
        connection,
        version_id,
        security_id,
        [
            MarketState.CONSOLIDATION,
            MarketState.ACCUMULATION,
            MarketState.BREAKOUT_READY,
            MarketState.CONSOLIDATION,
        ],
    )

    signals = assess_invalidation(connection, security_id, as_of=LATER)

    assert signals.backward_transitions == 1
    assert BACKWARD_TRANSITION in signals.signals_raised()
    assert signals.current_state is MarketState.CONSOLIDATION
    assert signals.last_backward_at == START + timedelta(weeks=3)


def test_a_clean_advance_raises_no_invalidation_signal(connection, register, version_id):
    security_id = register("CLEAN")
    _walk(
        connection,
        version_id,
        security_id,
        [MarketState.CONSOLIDATION, MarketState.ACCUMULATION, MarketState.BREAKOUT_WATCH],
    )

    signals = assess_invalidation(connection, security_id, as_of=LATER)

    assert signals.backward_transitions == 0
    assert signals.last_backward_at is None
    assert signals.signals_raised() == ()


def test_the_retreat_is_invisible_to_a_query_before_it_happened(connection, register, version_id):
    """History is bounded by `as_of`, so a replay in week two does not
    know about a breakdown in week four."""
    security_id = register("REPLAY")
    _walk(
        connection,
        version_id,
        security_id,
        [
            MarketState.CONSOLIDATION,
            MarketState.BREAKOUT_READY,
            MarketState.CONSOLIDATION,
        ],
    )

    early = assess_invalidation(connection, security_id, as_of=START + timedelta(weeks=1))
    late = assess_invalidation(connection, security_id, as_of=LATER)

    assert early.backward_transitions == 0
    assert early.current_state is MarketState.BREAKOUT_READY
    assert late.backward_transitions == 1


def test_losing_eligibility_is_not_counted_as_a_retreat(connection, register, version_id):
    """Module 10 decided UNCLASSIFIED sits outside the cycle: a security
    that stops being classifiable has had a change in what is *knowable*,
    not a failed breakout. This module inherits that and must not
    re-interpret it — the two invalidation signals stay separate."""
    security_id = register("UNCLASS")
    _walk(
        connection,
        version_id,
        security_id,
        [MarketState.BREAKOUT_READY, MarketState.UNCLASSIFIED],
    )

    signals = assess_invalidation(connection, security_id, as_of=LATER)

    assert signals.backward_transitions == 0
    assert signals.current_state is MarketState.UNCLASSIFIED


def test_no_state_history_is_reported_as_such_rather_than_as_stability(connection, register):
    security_id = register("NOHIST")

    signals = assess_invalidation(connection, security_id, as_of=LATER)

    assert signals.transitions_observed == 0
    assert signals.current_state is None
    assert signals.last_transition is None


# --------------------------------------------------------------------------
# Eligibility re-evaluation
# --------------------------------------------------------------------------


def test_a_security_that_passed_and_now_fails_surfaces_the_signal(
    connection, register, record_eligibility
):
    """The distinct, actionable case the Module 12 brief names.

    Passing in March and failing on liquidity in June means something
    happened to this security. A security that never passed carries no
    such information, and the trend distinguishes them.
    """
    security_id = register("LOST")
    record_eligibility(security_id, evaluated_at=datetime(2024, 3, 1, tzinfo=UTC))
    record_eligibility(
        security_id,
        evaluated_at=datetime(2024, 6, 1, tzinfo=UTC),
        failed=(EligibilityGate.LIQUIDITY,),
    )

    change = assess_eligibility_change(connection, security_id, as_of=LATER)

    assert change.trend is EligibilityTrend.LOST_ELIGIBILITY
    assert change.lost is True
    assert change.last_passing_at == datetime(2024, 3, 1, tzinfo=UTC)
    assert change.newly_failed_gates == (EligibilityGate.LIQUIDITY,)
    assert change.runs_observed == 2


def test_never_having_passed_is_not_an_invalidation(connection, register, record_eligibility):
    """Nothing was invalidated, because nothing was ever valid."""
    security_id = register("NEVEROK")
    record_eligibility(
        security_id,
        evaluated_at=datetime(2024, 3, 1, tzinfo=UTC),
        failed=(EligibilityGate.DATA_HISTORY,),
    )
    record_eligibility(
        security_id,
        evaluated_at=datetime(2024, 6, 1, tzinfo=UTC),
        failed=(EligibilityGate.DATA_HISTORY, EligibilityGate.LIQUIDITY),
    )

    change = assess_eligibility_change(connection, security_id, as_of=LATER)

    assert change.trend is EligibilityTrend.NEVER_ELIGIBLE
    assert change.lost is False
    assert change.last_passing_at is None
    # DATA_HISTORY was already failing; only LIQUIDITY is new.
    assert change.newly_failed_gates == (EligibilityGate.LIQUIDITY,)


def test_never_evaluated_is_distinct_from_evaluated_and_passing(
    connection, register, record_eligibility
):
    """A security Module 09 has never seen must not read as eligible."""
    unseen = register("UNSEEN")
    passing = register("PASSING")
    record_eligibility(passing, evaluated_at=datetime(2024, 6, 1, tzinfo=UTC))

    assert (
        assess_eligibility_change(connection, unseen, as_of=LATER).trend
        is EligibilityTrend.NEVER_EVALUATED
    )
    assert (
        assess_eligibility_change(connection, passing, as_of=LATER).trend
        is EligibilityTrend.PASSING
    )


def test_regaining_eligibility_reads_as_passing_again(connection, register, record_eligibility):
    """Recovery is not permanent invalidation. The trend reflects the
    latest verdict, and the history stays available for a caller that
    wants the whole arc."""
    security_id = register("RECOVER")
    record_eligibility(security_id, evaluated_at=datetime(2024, 1, 1, tzinfo=UTC))
    record_eligibility(
        security_id,
        evaluated_at=datetime(2024, 3, 1, tzinfo=UTC),
        failed=(EligibilityGate.LIQUIDITY,),
    )
    record_eligibility(security_id, evaluated_at=datetime(2024, 6, 1, tzinfo=UTC))

    change = assess_eligibility_change(connection, security_id, as_of=LATER)

    assert change.trend is EligibilityTrend.PASSING
    assert change.runs_observed == 3
    assert len(eligibility_history(connection, security_id)) == 3


def test_the_eligibility_bound_is_point_in_time(connection, register, record_eligibility):
    """A replay in April must not see June's failure."""
    security_id = register("PITELIG")
    record_eligibility(security_id, evaluated_at=datetime(2024, 3, 1, tzinfo=UTC))
    record_eligibility(
        security_id,
        evaluated_at=datetime(2024, 6, 1, tzinfo=UTC),
        failed=(EligibilityGate.LIQUIDITY,),
    )

    april = assess_eligibility_change(
        connection, security_id, as_of=datetime(2024, 4, 1, tzinfo=UTC)
    )

    assert april.trend is EligibilityTrend.PASSING
    assert april.runs_observed == 1


def test_a_partially_recorded_run_is_visible_as_partial(connection, register, record_eligibility):
    """A run that recorded three of six gates is not evidence of
    eligibility, and `gates_recorded` is how a caller can tell."""
    security_id = register("PARTIAL")
    record_eligibility(
        security_id,
        evaluated_at=datetime(2024, 6, 1, tzinfo=UTC),
        gates=(
            EligibilityGate.DATA_HISTORY,
            EligibilityGate.DATA_QUALITY,
            EligibilityGate.LIQUIDITY,
        ),
    )

    verdicts = eligibility_history(connection, security_id)

    assert len(verdicts) == 1
    assert verdicts[0].gates_recorded == 3
    assert verdicts[0].gates_recorded < len(tuple(EligibilityGate))


# --------------------------------------------------------------------------
# Both signals together
# --------------------------------------------------------------------------


def test_both_signals_are_reported_separately_and_never_blended(
    connection, register, version_id, record_eligibility
):
    """A security with both problems reports both by name.

    No combined invalidation number — that aggregation is Module 13's
    call, if it happens at all, and doing it here would hide which of the
    two fired.
    """
    security_id = register("BOTH")
    _walk(
        connection,
        version_id,
        security_id,
        [MarketState.BREAKOUT_READY, MarketState.CONSOLIDATION],
    )
    record_eligibility(security_id, evaluated_at=datetime(2024, 3, 1, tzinfo=UTC))
    record_eligibility(
        security_id,
        evaluated_at=datetime(2024, 6, 1, tzinfo=UTC),
        failed=(EligibilityGate.BANKRUPTCY_RISK,),
    )

    signals = assess_invalidation(connection, security_id, as_of=LATER)

    assert set(signals.signals_raised()) == {BACKWARD_TRANSITION, LOST_ELIGIBILITY}
    payload = signals.as_dict()
    assert payload["backward_transitions"] == 1
    assert payload["eligibility"]["trend"] == EligibilityTrend.LOST_ELIGIBILITY.value


def test_module_10_confidence_is_not_carried_into_the_risk_signals(
    connection, register, version_id
):
    """Module 10's report was explicit that its confidence is a
    pattern-match score from unvalidated weights. Recording it here would
    invite Module 13 to weight risk by it."""
    security_id = register("NOCONF")
    record_transitions(
        connection,
        ClassificationResult(
            as_of=START,
            target_model_version_id=version_id,
            assignments={
                security_id: StateAssignment(
                    security_id=security_id,
                    state=MarketState.CONSOLIDATION,
                    confidence=0.97,
                    evidence={"matched_state": MarketState.CONSOLIDATION.value},
                )
            },
        ),
    )

    payload = assess_invalidation(connection, security_id, as_of=LATER).as_dict()

    assert "confidence" not in payload
    assert all("confidence" not in str(key) for key in payload)
