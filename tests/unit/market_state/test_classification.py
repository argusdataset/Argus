"""Eligibility, refusal to classify, watchlist derivation, and batching.

The load-bearing claim here is the refusal. Module 09 spends six gates
deciding whether a security can be judged at all; if this module then
classifies an ineligible security anyway, those gates become decorative —
they would record a refusal that the very next stage silently overrode,
and nothing in the pipeline would surface the contradiction.

So `INSUFFICIENT_EVIDENCE` in, `UNCLASSIFIED` out, with the failing gate
names carried through so the security's absence from every watchlist is
traceable to a specific cause.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core.candidate_detection.eligibility.gates import (
    EligibilityOutcome,
    EligibilityReport,
    GateResult,
)
from core.data_validation.result import MissReason
from core.feature_engine.vector import BatchFeatureResult
from core.market_state import classify_states
from core.market_state.states import CLASSIFICATION_FEATURES, INTERNAL_STATES, WATCHLISTS
from data.canonical_model.records import CanonicalTimeframe
from infra.db.enums import EligibilityGate, MarketState
from tests.unit.candidate_detection.synthetic import build_universe
from tests.unit.feature_engine.lifecycle import LIFECYCLE_ID
from tests.unit.market_state.lifecycle import phase_vectors

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)


def _report(*, eligible: list, ineligible: dict) -> EligibilityReport:
    """An EligibilityReport with the given verdicts."""
    outcomes = {}
    for security_id in eligible:
        outcomes[security_id] = EligibilityOutcome(
            security_id=security_id,
            as_of=AS_OF,
            results={
                gate: GateResult(gate=gate, passed=True, detail={}) for gate in EligibilityGate
            },
        )
    for security_id, failed in ineligible.items():
        outcomes[security_id] = EligibilityOutcome(
            security_id=security_id,
            as_of=AS_OF,
            results={
                gate: GateResult(gate=gate, passed=gate not in failed, detail={})
                for gate in EligibilityGate
            },
        )
    return EligibilityReport(
        as_of=AS_OF, run_id=uuid4(), detection_configuration_id=None, outcomes=outcomes
    )


# --------------------------------------------------------------------------
# The refusal
# --------------------------------------------------------------------------


def test_an_ineligible_security_is_unclassified_not_force_classified():
    """The claim that keeps Module 09's gates from being decorative."""
    vectors = phase_vectors()["consolidation"]
    report = _report(eligible=[], ineligible={LIFECYCLE_ID: {EligibilityGate.LIQUIDITY}})

    assignment = classify_states(vectors, eligibility=report).assignments[LIFECYCLE_ID]

    assert assignment.state is MarketState.UNCLASSIFIED
    assert assignment.confidence is None


def test_the_unclassified_reason_names_the_failing_gate():
    """Traceable to a cause, not a generic "not classified".

    Without the gate name, a security's absence from every watchlist would
    be indistinguishable from a data gap, and diagnosing why the
    watchlists shrank would mean re-running Module 09.
    """
    vectors = phase_vectors()["consolidation"]
    report = _report(
        eligible=[],
        ineligible={LIFECYCLE_ID: {EligibilityGate.BANKRUPTCY_RISK, EligibilityGate.LIQUIDITY}},
    )

    reason = (
        classify_states(vectors, eligibility=report).assignments[LIFECYCLE_ID].unclassified_reason
    )

    assert reason.startswith("ineligible:")
    assert EligibilityGate.LIQUIDITY.value in reason
    assert EligibilityGate.BANKRUPTCY_RISK.value in reason


def test_the_same_security_classifies_normally_when_eligible():
    """The control. Without it, a classifier that refused everything passes."""
    vectors = phase_vectors()["consolidation"]
    report = _report(eligible=[LIFECYCLE_ID], ineligible={})

    assignment = classify_states(vectors, eligibility=report).assignments[LIFECYCLE_ID]
    assert assignment.state is not MarketState.UNCLASSIFIED


def test_an_ineligible_security_appears_on_no_watchlist():
    """The consequence that actually matters downstream."""
    vectors = phase_vectors()["consolidation"]
    report = _report(eligible=[], ineligible={LIFECYCLE_ID: {EligibilityGate.DATA_QUALITY}})
    result = classify_states(vectors, eligibility=report)

    for name in WATCHLISTS:
        assert LIFECYCLE_ID not in result.watchlist(name)


def test_a_security_module_08_could_not_compute_is_unclassified():
    """Missing vectors are reported, never silently dropped.

    Same discipline as Module 08's `missing_securities` and Module 09's
    `excluded`: a caller who asked about N securities can account for N.
    """
    absent = uuid4()
    features = BatchFeatureResult(
        as_of=AS_OF,
        feature_schema_version_id=None,
        timeframe=CanonicalTimeframe.DAILY,
        vectors={},
        missing_securities={absent: MissReason.NOT_YET_AVAILABLE},
    )

    assignment = classify_states(features).assignments[absent]
    assert assignment.state is MarketState.UNCLASSIFIED
    assert "no_feature_vector" in assignment.unclassified_reason


def test_a_security_with_no_usable_features_is_unclassified():
    """Thin evidence refuses classification rather than guessing."""
    vectors = phase_vectors()["consolidation"]
    blinded = dict(vectors.vectors[LIFECYCLE_ID].features)
    for name in CLASSIFICATION_FEATURES:
        blinded[name] = None

    vector = vectors.vectors[LIFECYCLE_ID]
    stripped = BatchFeatureResult(
        as_of=AS_OF,
        feature_schema_version_id=None,
        timeframe=CanonicalTimeframe.DAILY,
        vectors={
            LIFECYCLE_ID: type(vector)(
                security_id=LIFECYCLE_ID,
                as_of=AS_OF,
                feature_schema_version_id=None,
                timeframe=vector.timeframe,
                event_time=vector.event_time,
                availability_time=vector.availability_time,
                features=blinded,
                evidence=vector.evidence,
            )
        },
        missing_securities={},
    )

    assignment = classify_states(stripped).assignments[LIFECYCLE_ID]
    assert assignment.state is MarketState.UNCLASSIFIED
    assert assignment.unclassified_reason == "insufficient_features_for_any_state"


def test_thin_evidence_and_a_state_machine_gap_are_different_reasons():
    """Two very different failures must not share one label.

    "We could not judge this" and "our states do not cover this" are
    distinct problems — the first is a data issue, the second a modeling
    hole. Collapsing them would make the modeling hole permanently
    invisible, which is exactly how the CONSOLIDATION predicate bug
    survived until the lifecycle test ran.
    """
    import pandas as pd

    from core.market_state.classifier import _resolve_states
    from core.market_state.thresholds import StateThresholds

    # Full evidence, and every predicate deliberately failed. A choppy
    # sideways security at elevated volatility: at its highs, no
    # compression, no new lows, no breakout, nothing re-expanding.
    #
    # Mid-range values do NOT work here — at 0.5 across the board UPTREND
    # matches, since breakout_pct 0.5 clears zero and acceptance 0.5 clears
    # its floor. The predicates are permissive enough that landing outside
    # all of them takes construction, which is itself worth knowing.
    values = dict.fromkeys(CLASSIFICATION_FEATURES, 0.5)
    values["momentum_improvement"] = 1.0  # not decaying -> not DISTRIBUTION
    values["resistance_breakout_pct"] = -0.5  # below the level -> not UPTREND
    values["resistance_pressure"] = 0.1  # off the highs -> not BREAKOUT_*
    values["volatility_reexpansion"] = 0.5  # nothing waking -> not BREAKOUT_WATCH
    values["atr_percentile"] = 0.9  # loud -> not CONSOLIDATION/ACCUMULATION
    values["downside_momentum_reduction"] = -0.5  # not stabilizing -> not BASE_FORMING
    values["structure_transition"] = 1.0  # not falling -> not DOWN_TREND

    impossible = pd.DataFrame({k: [v] for k, v in values.items()}, index=[uuid4()])
    states, _, reasons = _resolve_states(impossible, StateThresholds())

    assert states.iloc[0] is MarketState.UNCLASSIFIED
    assert set(reasons.values()) == {"no_state_predicate_matched"}


# --------------------------------------------------------------------------
# Watchlists are derived, never stored
# --------------------------------------------------------------------------


def test_the_three_watchlists_are_disjoint():
    """No state appears on two lists."""
    seen: set[MarketState] = set()
    for states in WATCHLISTS.values():
        assert not (seen & states)
        seen |= states


def test_the_watchlists_cover_exactly_the_six_public_states():
    """Six public states on three lists; three internal states on none."""
    covered = set().union(*WATCHLISTS.values())
    assert covered == set(MarketState) - INTERNAL_STATES
    assert len(covered) == 6


def test_an_unknown_watchlist_name_raises_rather_than_returning_empty():
    """A typo must not look like a quiet day.

    An empty list is a legitimate answer ("nothing is consolidating
    today"), so a misspelled name returning one would be indistinguishable
    from a real result.
    """
    result = classify_states(phase_vectors()["consolidation"])
    with pytest.raises(KeyError, match="Unknown watchlist"):
        result.watchlist("BREAKOUT")


def test_watchlist_membership_is_computed_from_state_not_stored():
    """A structural check: the result object holds states, not lists.

    Per the Source-of-Truth principle, a stored watchlist would be a
    second place membership is recorded and a second thing that could
    disagree with `market_state`.
    """
    result = classify_states(phase_vectors()["consolidation"])
    fields = set(type(result).__dataclass_fields__)

    assert fields == {"as_of", "target_model_version_id", "assignments"}
    assert not any("watchlist" in name for name in fields)


# --------------------------------------------------------------------------
# Batch
# --------------------------------------------------------------------------


def test_a_full_universe_classifies_in_one_call():
    """400 securities, one pass, every one accounted for."""
    universe = build_universe(size=400)
    result = classify_states(universe)

    assert len(result) == len(universe.vectors)
    assert set(result.assignments) == set(universe.vectors)


def test_every_security_receives_exactly_one_state():
    universe = build_universe(size=200)
    result = classify_states(universe)

    distribution = result.distribution()
    assert sum(distribution.values()) == len(result)
    assert set(distribution) == set(MarketState)


def test_classification_performs_no_io():
    """It takes feature vectors, not a connection.

    Asserted structurally, the same way Module 09's detection stage is:
    a classifier that queried the database would make "is this vectorized"
    unanswerable by reading it.
    """
    import inspect

    from core.market_state import classifier

    signature = inspect.signature(classifier.classify_states)
    assert "connection" not in signature.parameters
    assert "Connection" not in inspect.getsource(classifier)


def test_classification_cost_is_amortized_across_the_universe():
    """Vectorized: per-security cost falls as the batch grows.

    Same measurement Module 08's suite uses, and the same caveat — the
    arithmetic is genuinely O(securities), so what this asserts is that
    the *fixed* cost is amortized rather than paid per security.
    """
    import time

    small = build_universe(size=50)
    large = build_universe(size=800)

    def per_security(universe) -> float:
        classify_states(universe)  # warm
        started = time.perf_counter()
        classify_states(universe)
        return (time.perf_counter() - started) / len(universe.vectors)

    assert per_security(large) < per_security(small)
