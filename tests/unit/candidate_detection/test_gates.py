"""The gates, individually — especially the two that pull against each other.

The bankruptcy gate and the liquidity gate are in direct tension with
ARGUS's purpose, in opposite directions:

- Too loose on bankruptcy and a company dying quietly gets scored as a
  base, because that is exactly what the price action looks like.
- Too tight on liquidity and the microcaps ARGUS was built to find are
  deleted before anything ever looks at them — silently, and while
  appearing rigorous.

The prompt calls the liquidity test as important as the bankruptcy one,
and it is: an over-strict floor defeats the project's actual purpose,
and nothing downstream would ever surface that it had happened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core.candidate_detection.config import EligibilityParameters
from core.candidate_detection.eligibility.bankruptcy import (
    DistressSignals,
    evaluate_bankruptcy_gate,
)
from core.candidate_detection.eligibility.checks import (
    CRITICAL_FEATURES,
    check_data_history,
    check_data_quality,
    check_liquidity,
    check_valid_asset_identity,
)
from core.candidate_detection.eligibility.gates import EligibilityOutcome, GateResult
from core.data_validation.result import MissReason
from core.data_validation.universe import MembershipAsOf
from core.universe.intervals import IntervalEvidence
from data.canonical_model.exchanges import CanonicalExchange
from infra.db.enums import EligibilityGate, EvidenceStatus, ListingStatus
from tests.unit.candidate_detection.synthetic import (
    SPARSE_ID,
    THIN_SMALLCAP_ID,
    UNTRADEABLE_ID,
    build_universe,
)

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)


@pytest.fixture(scope="module")
def universe():
    return build_universe()


@pytest.fixture(scope="module")
def parameters() -> EligibilityParameters:
    return EligibilityParameters()


# --------------------------------------------------------------------------
# LIQUIDITY — the gate that must not delete ARGUS's target population
# --------------------------------------------------------------------------


def test_a_thinly_traded_small_cap_passes_the_liquidity_gate(universe, parameters):
    """The test that protects the project's purpose.

    $90,000 of average daily dollar volume is genuinely thin — the profile
    of the real names discussed as motivating examples during their basing
    periods. It is also perfectly tradeable at retail size. A floor that
    rejected this would look disciplined and would quietly remove the
    entire population ARGUS exists to find.
    """
    vector = universe.vectors[THIN_SMALLCAP_ID]
    result = check_liquidity(vector, parameters)

    assert result.passed
    assert result.detail["avg_dollar_volume"] == pytest.approx(90_000.0)


def test_the_floor_still_excludes_execution_impossible_names(universe, parameters):
    """The floor is not decorative. $8,000/day cannot absorb a position."""
    result = check_liquidity(universe.vectors[UNTRADEABLE_ID], parameters)
    assert not result.passed
    assert result.detail["avg_dollar_volume"] == pytest.approx(8_000.0)


def test_the_floor_is_low_enough_to_admit_a_microcap_base(parameters):
    """Stated as a property of the number, not of one fixture.

    At the configured floor, a $2,500 position is at most 5% of a day's
    turnover — the reasoning in `check_liquidity`'s docstring, asserted so
    that raising the floor without revisiting that argument fails here.
    """
    retail_position = 2_500.0
    assert retail_position / parameters.min_avg_dollar_volume <= 0.05


def test_an_unmeasurable_liquidity_fails_rather_than_passing(universe, parameters):
    """Absence of evidence is not evidence of tradeability."""
    vector = universe.vectors[THIN_SMALLCAP_ID]
    blinded = _with_features(vector, {"avg_dollar_volume": None})
    result = check_liquidity(blinded, parameters)

    assert not result.passed
    assert result.detail["measurable"] is False


# --------------------------------------------------------------------------
# BANKRUPTCY_RISK — the gate that must not delete it either
# --------------------------------------------------------------------------


def test_two_signals_exclude(parameters):
    """A company with negative equity and no runway is not basing."""
    signals = DistressSignals(
        uuid4(),
        fired=("negative_shareholders_equity", "short_cash_runway"),
        measured={"shareholders_equity": -4_000_000.0, "runway_quarters": 1.2},
        evaluated=True,
    )
    assert not evaluate_bankruptcy_gate(signals, parameters).passed


def test_one_signal_does_not_exclude(parameters):
    """A clinical-stage biotech burning cash is doing its job, not dying.

    The single most important calibration decision in this gate. Firing on
    one signal would systematically strip out pre-revenue small caps —
    which is to say, the target population — while feeling rigorous.
    """
    signals = DistressSignals(
        uuid4(),
        fired=("short_cash_runway",),
        measured={"runway_quarters": 3.1},
        evaluated=True,
    )
    result = evaluate_bankruptcy_gate(signals, parameters)

    assert result.passed
    # But the concern is still recorded, so a later module can see that it
    # existed and was consciously not disqualifying.
    assert result.detail["signals_fired"] == ["short_cash_runway"]
    assert result.detail["signal_count"] == 1


def test_a_security_with_no_fundamentals_passes_this_gate_and_says_so(parameters):
    """Missing fundamentals is a coverage fact, not a bankruptcy verdict.

    Adjudicating it here would both duplicate the DATA_QUALITY gate and
    convert "we don't know" into "this company is dying" — which for a
    universe full of small caps with patchy filings would be catastrophic.
    """
    signals = DistressSignals(uuid4(), (), {"reason": "no_fundamentals"}, evaluated=False)
    result = evaluate_bankruptcy_gate(signals, parameters)

    assert result.passed
    assert result.detail["evaluated"] is False


def test_the_gate_records_which_proxy_judged_it(parameters):
    """The proxy is explicitly provisional, so stored rows must name it."""
    signals = DistressSignals(uuid4(), (), {}, evaluated=True)
    assert evaluate_bankruptcy_gate(signals, parameters).detail["proxy"] == (
        "fundamentals-distress-v1"
    )


def test_the_exclusion_threshold_is_configurable(universe):
    """A stricter operator can demand one signal; the default does not."""
    signals = DistressSignals(uuid4(), ("extreme_leverage",), {}, evaluated=True)
    strict = EligibilityParameters(min_distress_signals_to_exclude=1)

    assert evaluate_bankruptcy_gate(signals, EligibilityParameters()).passed
    assert not evaluate_bankruptcy_gate(signals, strict).passed


# --------------------------------------------------------------------------
# DATA_HISTORY and DATA_QUALITY
# --------------------------------------------------------------------------


def test_a_recently_listed_security_fails_data_history(universe, parameters):
    """30 bars cannot fill a 252-bar window, and must not pretend to."""
    result = check_data_history(universe.vectors[SPARSE_ID], parameters)

    assert not result.passed
    assert result.detail["bars_available"] == 30
    assert result.detail["bars_required"] == 252


def test_a_seasoned_security_passes_data_history(universe, parameters):
    assert check_data_history(universe.vectors[THIN_SMALLCAP_ID], parameters).passed


def test_the_history_gate_is_relative_to_the_spec_not_a_hardcoded_bar_count(universe, parameters):
    """Changing a Module 08 window must not silently change this gate.

    `bars_required` is read off the vector's own evidence, which came from
    the spec. A gate that hardcoded 252 would drift the moment the spec
    moved, with nothing to catch it.
    """
    vector = universe.vectors[SPARSE_ID]
    assert vector.evidence.bars_required == 252
    detail = check_data_history(vector, parameters).detail
    assert detail["bars_required"] == vector.evidence.bars_required


def test_missing_critical_features_fail_data_quality(universe, parameters):
    """Completeness alone would let a vector through that is missing the point.

    A security can clear a 70% completeness bar while lacking precisely
    the measurements every downstream module depends on.
    """
    vector = universe.vectors[THIN_SMALLCAP_ID]
    blinded = _with_features(vector, {"drawdown_pct": None})
    result = check_data_quality(blinded, parameters)

    assert not result.passed
    assert "drawdown_pct" in result.detail["missing_critical_features"]


def test_data_quality_carries_the_miss_reasons_module_08_recorded(universe, parameters):
    """The MissReason taxonomy survives into the gate's explanation.

    This is what makes INSUFFICIENT_EVIDENCE a diagnosis rather than a
    shrug — the rejection names the absent input and why it was absent.
    """
    result = check_data_quality(universe.vectors[THIN_SMALLCAP_ID], parameters)
    assert result.detail["missing_inputs"]["sector_benchmark"] == (MissReason.NEVER_INGESTED.value)


def test_absent_sector_features_alone_do_not_fail_data_quality(universe, parameters):
    """No sector data exists in the schema. Rejecting for it rejects everything.

    Module 08 reports sector features as NaN with NEVER_INGESTED; treating
    that as a data-quality failure would take the entire universe out on a
    gap that is nobody's fault and that no candidate could ever fix.
    """
    result = check_data_quality(universe.vectors[THIN_SMALLCAP_ID], parameters)
    assert result.passed
    assert not any("sector" in name for name in CRITICAL_FEATURES)


# --------------------------------------------------------------------------
# VALID_ASSET_IDENTITY
# --------------------------------------------------------------------------


def _membership(
    status: ListingStatus = ListingStatus.LISTED,
    from_evidence: IntervalEvidence = IntervalEvidence.PRICE_HISTORY,
    to_evidence: IntervalEvidence = IntervalEvidence.PRICE_HISTORY,
) -> MembershipAsOf:
    return MembershipAsOf(
        security_id=uuid4(),
        universe_version_id=uuid4(),
        listing_status=status,
        exchange=CanonicalExchange.NASDAQ,
        listed_from=datetime(2015, 1, 1, tzinfo=UTC),
        listed_to=None,
        from_evidence=from_evidence,
        to_evidence=to_evidence,
    )


def test_a_listed_security_with_solid_boundaries_passes():
    assert check_valid_asset_identity(uuid4(), _membership(), AS_OF).passed


def test_a_security_with_no_membership_row_fails_with_a_named_reason():
    result = check_valid_asset_identity(
        uuid4(), None, AS_OF, miss_reason=MissReason.OUTSIDE_INTERVAL
    )
    assert not result.passed
    assert result.detail["reason"] == MissReason.OUTSIDE_INTERVAL.value


def test_a_delisted_security_fails_the_live_identity_gate():
    """Retained in the universe, excluded as a candidate — different questions.

    Module 03's enum comment is explicit that delisted securities stay in
    the universe or backtests inherit survivorship bias. Whether one is a
    tradeable candidate *today* is a separate decision, made here.
    """
    result = check_valid_asset_identity(uuid4(), _membership(ListingStatus.DELISTED), AS_OF)
    assert not result.passed
    assert result.detail["listing_status"] == ListingStatus.DELISTED.value


def test_guessed_interval_boundaries_fail_the_gate():
    """A `missing` boundary means ARGUS does not know when it was listed.

    Answering "was it listed at as_of" from a guessed interval would let
    survivorship assumptions back in through the identity gate.
    """
    result = check_valid_asset_identity(
        uuid4(), _membership(from_evidence=IntervalEvidence.MISSING), AS_OF
    )
    assert not result.passed
    assert result.detail["boundaries_known"] is False


def test_weak_but_real_evidence_passes_and_is_recorded():
    """`first_observed` is weak, not absent — allowed through, with the fact kept."""
    result = check_valid_asset_identity(
        uuid4(), _membership(from_evidence=IntervalEvidence.FIRST_OBSERVED), AS_OF
    )
    assert result.passed
    assert result.detail["from_evidence"] == IntervalEvidence.FIRST_OBSERVED.value


# --------------------------------------------------------------------------
# The outcome type
# --------------------------------------------------------------------------


def test_a_failed_gate_produces_insufficient_evidence_not_a_score():
    """The distinction the whole module exists to preserve."""
    outcome = _outcome({EligibilityGate.LIQUIDITY: False})

    assert not outcome.eligible
    assert outcome.status is EvidenceStatus.INSUFFICIENT_EVIDENCE
    assert outcome.failed_gates == (EligibilityGate.LIQUIDITY,)


def test_a_fully_passing_candidate_is_merely_permitted_to_be_scored():
    """`SCORED` means *may be scored*. Module 13 assigns the numbers."""
    outcome = _outcome({})
    assert outcome.eligible
    assert outcome.status is EvidenceStatus.SCORED


def test_every_failing_gate_is_reported_not_just_the_first():
    """ "Failed liquidity" and "failed liquidity, bankruptcy and history"
    are different facts, and only the second says the name is hopeless.
    """
    outcome = _outcome(
        {
            EligibilityGate.LIQUIDITY: False,
            EligibilityGate.BANKRUPTCY_RISK: False,
            EligibilityGate.DATA_HISTORY: False,
        }
    )
    assert set(outcome.failed_gates) == {
        EligibilityGate.LIQUIDITY,
        EligibilityGate.BANKRUPTCY_RISK,
        EligibilityGate.DATA_HISTORY,
    }
    assert set(outcome.reason_summary()) == {gate.value for gate in outcome.failed_gates}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _with_features(vector, overrides):
    """A copy of `vector` with some features replaced."""
    features = {**vector.features, **overrides}
    unavailable = tuple(name for name, value in features.items() if value is None)
    evidence = type(vector.evidence)(
        bars_available=vector.evidence.bars_available,
        bars_required=vector.evidence.bars_required,
        missing_inputs=dict(vector.evidence.missing_inputs),
        unavailable_features=unavailable,
    )
    return type(vector)(
        security_id=vector.security_id,
        as_of=vector.as_of,
        feature_schema_version_id=None,
        timeframe=vector.timeframe,
        event_time=vector.event_time,
        availability_time=vector.availability_time,
        features=features,
        evidence=evidence,
    )


def _outcome(failures: dict[EligibilityGate, bool]) -> EligibilityOutcome:
    return EligibilityOutcome(
        security_id=uuid4(),
        as_of=AS_OF,
        results={
            gate: GateResult(gate=gate, passed=failures.get(gate, True), detail={})
            for gate in EligibilityGate
        },
    )
