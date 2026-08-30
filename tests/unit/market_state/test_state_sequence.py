"""Does a security walking the setup move through states in a sane order?

This is the closest thing Module 10 has to a correctness test, and it is
deliberately weaker than the tests in Modules 03-09. It has to be: the
thresholds are unvalidated placeholders, so "BASE_FORMING at bar 430" is
not a fact anyone can assert. What *can* be asserted is direction and
sequence — a security that declines, quiets, coils and then breaks out
must not be classified UPTREND during its decline, or DOWN_TREND after it
has cleared resistance.

So every assertion here is about *ordering along the cycle*, never about
which specific state a phase lands in. That is the honest limit of what
this module can currently claim, and writing the tests any tighter would
be encoding the placeholder thresholds as though they were validated.

The measured sequence on the shared lifecycle fixture is recorded in the
module README, so a threshold change that reshuffles it is visible in a
diff rather than only in a failure.
"""

from __future__ import annotations

import pytest

from core.market_state import classify_states
from core.market_state.transitions import CYCLE_ORDER
from infra.db.enums import MarketState
from tests.unit.feature_engine.lifecycle import LIFECYCLE_ID
from tests.unit.market_state.lifecycle import PHASE_ORDER, phase_vectors


@pytest.fixture(scope="module")
def sequence() -> dict[str, MarketState]:
    """The state assigned at the end of each phase."""
    vectors = phase_vectors()
    return {
        phase: classify_states(vectors[phase]).assignments[LIFECYCLE_ID].state
        for phase in PHASE_ORDER
    }


def _position(state: MarketState) -> int:
    return CYCLE_ORDER.index(state)


# --------------------------------------------------------------------------
# The sequence
# --------------------------------------------------------------------------


def test_every_phase_is_classified(sequence):
    """A security with full history must land somewhere.

    UNCLASSIFIED here would mean the state machine has a hole — evidence
    present, no predicate matched — which `_resolve_states` names
    `no_state_predicate_matched` precisely so it cannot hide. An earlier
    version of the CONSOLIDATION predicate did exactly this: it tested
    change in volatility rather than level, and a fully-formed base
    matched no state at all.
    """
    for phase, state in sequence.items():
        assert state is not MarketState.UNCLASSIFIED, f"{phase} fell through every predicate"


def test_the_decline_is_a_down_trend(sequence):
    assert sequence["decline"] is MarketState.DOWN_TREND


def test_the_base_reads_as_a_consolidation_family_state(sequence):
    """CONSOLIDATION or ACCUMULATION — which of the two is a threshold call.

    Asserting the family rather than the exact state is the point. Whether
    a given base has "enough" rising lows to count as accumulation is
    precisely the judgement no one has calibrated yet.
    """
    assert sequence["consolidation"] in {
        MarketState.CONSOLIDATION,
        MarketState.ACCUMULATION,
    }


def test_the_awakening_reads_as_a_breakout_family_state(sequence):
    assert sequence["awakening"] in {
        MarketState.BREAKOUT_WATCH,
        MarketState.BREAKOUT_READY,
    }


def test_the_confirmation_reads_as_an_uptrend(sequence):
    """Through the level and holding."""
    assert sequence["confirmation"] is MarketState.UPTREND


def test_the_setup_progresses_forward_through_the_cycle(sequence):
    """The central claim: decline → base → awakening → breakout, in order.

    Compared by position in `CYCLE_ORDER` rather than by naming states, so
    a recalibration that shifts *which* state each phase lands in still
    passes as long as the progression holds. That is the property that
    should survive calibration; the specific states are not.
    """
    phases = ["decline", "stabilization", "consolidation", "awakening", "confirmation"]
    positions = [_position(sequence[phase]) for phase in phases]

    assert positions == sorted(positions), (
        f"states moved backwards through the setup: {[(p, sequence[p].value) for p in phases]}"
    )


def test_the_decline_never_reads_as_a_breakout(sequence):
    """The error that would matter most, stated directly."""
    assert sequence["decline"] not in {
        MarketState.BREAKOUT_WATCH,
        MarketState.BREAKOUT_READY,
        MarketState.UPTREND,
    }


def test_the_confirmation_never_reads_as_a_decline(sequence):
    assert sequence["confirmation"] not in {
        MarketState.DOWN_TREND,
        MarketState.BASE_FORMING,
    }


# --------------------------------------------------------------------------
# Watchlist membership follows from the sequence
# --------------------------------------------------------------------------


def test_the_base_appears_on_the_consolidation_watchlist(sequence):
    vectors = phase_vectors()
    result = classify_states(vectors["consolidation"])
    assert LIFECYCLE_ID in result.watchlist("CONSOLIDATION")


def test_the_awakening_appears_on_the_breakout_ready_watchlist(sequence):
    vectors = phase_vectors()
    result = classify_states(vectors["awakening"])
    assert LIFECYCLE_ID in result.watchlist("BREAKOUT_READY")


def test_a_security_appears_on_at_most_one_watchlist(sequence):
    """The four lists partition seven of the nine states, disjointly.

    Overlap would make "how many securities are consolidating" ambiguous
    and would let one security be counted twice in any summary.
    """
    vectors = phase_vectors()
    for phase in PHASE_ORDER:
        result = classify_states(vectors[phase])
        memberships = [
            name
            for name in ("DOWN_TREND", "CONSOLIDATION", "BREAKOUT_READY", "UPTREND")
            if LIFECYCLE_ID in result.watchlist(name)
        ]
        assert len(memberships) <= 1, f"{phase} appears on {memberships}"


def test_the_uptrend_appears_on_its_own_confirmed_moves_watchlist(sequence):
    """UPTREND is the fourth watchlist, not an internal state.

    A security that already broke out is exactly the evidence ARGUS most
    wants to show — a candidate it called correctly, now visibly moving —
    so it is surfaced on its own list rather than disappearing from every
    watchlist the moment it confirms.
    """
    vectors = phase_vectors()
    result = classify_states(vectors["confirmation"])
    assert sequence["confirmation"] is MarketState.UPTREND
    assert LIFECYCLE_ID in result.watchlist("UPTREND")
    for name in ("DOWN_TREND", "CONSOLIDATION", "BREAKOUT_READY"):
        assert LIFECYCLE_ID not in result.watchlist(name)


def test_a_reversal_out_of_uptrend_leaves_the_confirmed_moves_watchlist(sequence):
    """A genuine reversal back toward DOWN_TREND removes it from UPTREND.

    This is a live, derived view like the other three — not a permanent
    hall of fame. The security's full history stays queryable through
    Module 10's transition log regardless of current membership.
    """
    vectors = phase_vectors()
    confirmed = classify_states(vectors["confirmation"])
    assert LIFECYCLE_ID in confirmed.watchlist("UPTREND")

    reversed_result = classify_states(vectors["decline"])
    assert LIFECYCLE_ID not in reversed_result.watchlist("UPTREND")


# --------------------------------------------------------------------------
# Dual mode
# --------------------------------------------------------------------------


def test_classifying_each_phase_is_the_same_call_with_a_different_as_of(sequence):
    """The dual-mode guarantee, exercised rather than asserted.

    Six classifications, one function, one argument different. There is no
    live path and no replay path — which is what
    `CROSS_CUTTING_REQUIREMENTS.md` asks Modules 08-16 to guarantee.
    """
    vectors = phase_vectors()
    as_ofs = [vectors[phase].as_of for phase in PHASE_ORDER]

    assert len(set(as_ofs)) == len(as_ofs), "each phase must have a distinct as_of"
    assert as_ofs == sorted(as_ofs)

    for phase in PHASE_ORDER:
        result = classify_states(vectors[phase])
        assert result.as_of == vectors[phase].as_of


def test_classification_is_deterministic():
    """Same features, same states — a prerequisite for reproducible replay."""
    vectors = phase_vectors()
    first = classify_states(vectors["consolidation"]).states()
    second = classify_states(vectors["consolidation"]).states()
    assert first == second
