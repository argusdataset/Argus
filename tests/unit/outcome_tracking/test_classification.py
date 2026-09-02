"""Every outcome status and every false-positive type, one case each."""

from __future__ import annotations

import pytest

from core.outcome_tracking.classification import (
    COINCIDENT,
    ClassificationInputs,
    classify,
)
from core.outcome_tracking.config import OutcomeThresholds
from core.outcome_tracking.excursion import Excursion
from infra.db.enums import (
    FalsePositiveType,
    OutcomeStatus,
    ReviewConfidence,
    SetupLifecycleStatus,
)
from tests.unit.outcome_tracking import factories as make

THRESHOLDS = OutcomeThresholds()


def _classify(**kwargs):
    return classify(make.inputs(**kwargs), THRESHOLDS)


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


def test_the_target_reached_first_is_a_success():
    result = _classify(target_day=20, stop_day=None)

    assert result.status is OutcomeStatus.SUCCESS
    assert result.false_positive_type is None
    assert result.review_confidence is ReviewConfidence.HIGH


def test_the_stop_reached_first_is_a_failure():
    result = _classify(target_day=40, stop_day=10, realized=-0.06, mfe=0.02)

    assert result.status is OutcomeStatus.FAILED
    assert result.review_confidence is ReviewConfidence.HIGH


def test_both_thresholds_on_one_bar_resolve_as_failed():
    """Daily bars do not record intrabar order, so the adverse case is
    assumed. It can only understate the pattern's performance, and a
    dataset that flatters the thing it exists to test is worthless."""
    result = _classify(target_day=15, stop_day=15, realized=-0.02, mfe=0.11)

    assert result.status is OutcomeStatus.FAILED
    assert "same bar" in result.reason


def test_an_unresolved_criterion_after_an_endpoint_is_expired():
    """The structure resolved but the predefined criterion did not. That
    is a real, informative non-event — not a quiet success."""
    result = _classify(target_day=None, stop_day=None, mfe=0.04, realized=0.01)

    assert result.status is OutcomeStatus.EXPIRED


def test_an_unresolved_criterion_after_an_invalidation_is_invalidated():
    result = _classify(
        terminal_event_type=make.INVALIDATED_LOST,
        target_day=None,
        stop_day=None,
        mfe=0.04,
        realized=0.01,
    )

    assert result.status is OutcomeStatus.INVALIDATED
    assert result.review_confidence is ReviewConfidence.MEDIUM


def test_a_setup_that_never_activated_has_no_valid_outcome():
    """No entry point means no return to measure. The row still exists and
    still explains itself."""
    result = classify(
        ClassificationInputs(
            excursion=Excursion(unavailable=("no_entry_event",)),
            terminal_event_type=make.EXPIRED_UNQUALIFIED,
            status_before=SetupLifecycleStatus.DETECTION,
            activated_at=None,
        ),
        THRESHOLDS,
    )

    assert result.status is OutcomeStatus.NO_VALID_OUTCOME
    assert result.false_positive_type is FalsePositiveType.A_NO_PATTERN
    assert "never activated" in result.reason
    assert result.review_confidence is ReviewConfidence.LOW


def test_an_activated_setup_with_no_measurable_excursion_is_no_valid_outcome():
    result = _classify(measured=False)

    assert result.status is OutcomeStatus.NO_VALID_OUTCOME
    assert result.review_confidence is ReviewConfidence.LOW


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (make.EXPIRED_ACTIVE, OutcomeStatus.EXPIRED),
        (make.INVALIDATED_LOST, OutcomeStatus.INVALIDATED),
        (make.INVALIDATED_INELIGIBLE, OutcomeStatus.INVALIDATED),
    ],
)
def test_the_terminal_event_mapping_applies_when_the_criterion_is_silent(event_type, expected):
    """Module 14's mapping is the fallback, and in the ordinary case it is
    what runs."""
    result = _classify(terminal_event_type=event_type, target_day=None, stop_day=None, mfe=0.04)

    assert result.status is expected


def test_the_criterion_outranks_the_terminal_event_when_they_disagree():
    """The divergence the literal mapping gets wrong.

    A setup can reach the target on day thirty and still be closed by
    Module 14's expiry or by an administrative invalidation — Module 14
    watches market states and eligibility, not price. Labelling that
    EXPIRED would put a resolved success into the dataset as an
    unresolved non-event, and Module 17's hit rate would be too low.
    """
    expired = _classify(terminal_event_type=make.EXPIRED_ACTIVE, target_day=20)
    invalidated = _classify(terminal_event_type=make.INVALIDATED_LOST, target_day=20)

    assert expired.status is OutcomeStatus.SUCCESS
    assert invalidated.status is OutcomeStatus.SUCCESS


# --------------------------------------------------------------------------
# The A-G taxonomy
# --------------------------------------------------------------------------


def test_a_success_carries_no_false_positive_type():
    assert _classify(target_day=20).false_positive_type is None


def test_type_a_is_a_setup_that_never_developed():
    result = classify(
        ClassificationInputs(
            excursion=Excursion(unavailable=("no_entry_event",)),
            terminal_event_type=make.EXPIRED_UNQUALIFIED,
            status_before=SetupLifecycleStatus.DETECTION,
            activated_at=None,
        ),
        THRESHOLDS,
    )
    assert result.false_positive_type is FalsePositiveType.A_NO_PATTERN


def test_type_b_is_a_pattern_that_went_nowhere():
    result = _classify(target_day=None, stop_day=None, mfe=0.01, realized=0.0)

    assert result.false_positive_type is FalsePositiveType.B_PATTERN_NO_EXPANSION
    assert "went nowhere" in result.false_positive_reason


def test_type_c_is_a_breakout_that_did_not_hold():
    """Moved and failed is a different lesson from never having moved, and
    the expansion floor is what separates them."""
    result = _classify(target_day=None, stop_day=10, mfe=0.08, realized=-0.06)

    assert result.false_positive_type is FalsePositiveType.C_FALSE_BREAKOUT


def test_type_d_is_a_base_that_broke_down():
    result = _classify(target_day=None, stop_day=5, mfe=0.01, realized=-0.30)

    assert result.false_positive_type is FalsePositiveType.D_BREAKDOWN


# --------------------------------------------------------------------------
# The B/C/D boundaries scale with the security's own volatility
# --------------------------------------------------------------------------


def test_the_same_excursion_is_typed_differently_by_volatility():
    """The flaw this normalization fixes, made concrete.

    An identical 8% peak excursion is a real advance for a security whose
    session covers a fraction of a percent, and unremarkable noise for one
    that routinely swings 25%. Under a flat floor both were type C.
    """
    quiet = _classify(target_day=None, stop_day=10, mfe=0.08, realized=-0.01, atr_at_entry=1.0)
    volatile = _classify(target_day=None, stop_day=10, mfe=0.08, realized=-0.01, atr_at_entry=25.0)

    # Floors are 0.45x and -2.25x of ATR/entry_price, on entry_price=100:
    #   quiet    -> expansion floor 0.0045, so 8% cleared it
    #   volatile -> expansion floor 0.1125, so 8% did not
    # The realized return stays inside both breakdown floors, so type D
    # (checked first) does not pre-empt the comparison under test.
    assert quiet.false_positive_type is FalsePositiveType.C_FALSE_BREAKOUT
    assert volatile.false_positive_type is FalsePositiveType.B_PATTERN_NO_EXPANSION


def test_the_breakdown_floor_scales_too():
    """A 30% fall is a collapse for a stable name and an ordinary week for
    a violent one. Only the first is a type D."""
    quiet = _classify(target_day=None, stop_day=5, mfe=0.01, realized=-0.30, atr_at_entry=5.0)
    volatile = _classify(target_day=None, stop_day=5, mfe=0.01, realized=-0.30, atr_at_entry=20.0)

    # Breakdown floors: -0.1125 for the quiet name, -0.45 for the volatile one.
    assert quiet.false_positive_type is FalsePositiveType.D_BREAKDOWN
    assert volatile.false_positive_type is not FalsePositiveType.D_BREAKDOWN


def test_the_atr_multiples_reproduce_the_flat_floors_they_replaced():
    """The translation moved no boundary — asserted, not assumed.

    At the fixture's entry ATR the two multiples resolve to exactly the
    0.03 and -0.15 that used to be written as flat percentages. That is
    what makes every other test in this file still test what it tested
    before the units changed.
    """
    fraction = make.ENTRY_ATR / make.ENTRY_PRICE

    assert THRESHOLDS.expansion_atr_multiple.value * fraction == pytest.approx(0.03)
    assert THRESHOLDS.breakdown_atr_multiple.value * fraction == pytest.approx(-0.15)


def test_without_an_entry_atr_no_type_is_asserted():
    """No volatility scale means "did this move at all" has no answer.

    Defaulting to type B would assert the structure went nowhere on the
    strength of a measurement that does not exist. The row is left
    unclassified and says why, and review confidence drops to LOW.
    """
    result = _classify(
        target_day=None,
        stop_day=None,
        mfe=0.01,
        realized=0.0,
        atr_at_entry=None,
        unavailable=("no_atr_at_entry",),
    )

    assert result.false_positive_type is None
    assert "entry ATR was not measurable" in result.false_positive_reason
    assert result.review_confidence is ReviewConfidence.LOW


def test_type_e_is_a_move_that_coincided_with_a_scheduled_event():
    """The fixture the brief asks for by name: a coincident earnings date
    classifies as E rather than being left ambiguous."""
    result = _classify(
        target_day=None,
        stop_day=10,
        mfe=0.08,
        mfe_day=20,
        realized=-0.06,
        events=(make.earnings_event(day=20),),
    )

    assert result.false_positive_type is FalsePositiveType.E_CATALYST_DRIVEN
    assert result.false_positive_confidence == COINCIDENT
    assert "EARNINGS" in result.false_positive_reason


def test_an_event_far_from_the_excursion_is_not_type_e():
    """Earnings somewhere in a sixty-day window is unremarkable. Earnings
    on the day the move happened is what type E is about."""
    result = _classify(
        target_day=None,
        stop_day=10,
        mfe=0.08,
        mfe_day=20,
        realized=-0.06,
        events=(make.earnings_event(day=55),),
    )

    assert result.false_positive_type is not FalsePositiveType.E_CATALYST_DRIVEN


def test_type_f_is_an_illiquid_print():
    result = _classify(
        target_day=None, stop_day=10, mfe=0.08, realized=-0.06, avg_dollar_volume=40_000.0
    )

    assert result.false_positive_type is FalsePositiveType.F_ILLIQUID_DISTORTION


def test_type_g_is_a_coincident_corporate_action():
    result = _classify(
        target_day=None, stop_day=10, mfe=0.08, realized=-0.06, actions=(make.split_action(),)
    )

    assert result.false_positive_type is FalsePositiveType.G_CORPORATE_ACTION_DISTORTION


def test_distortions_outrank_structural_verdicts():
    """A base that "failed" through a split artefact did not fail as a
    pattern. Recording it as a type-D breakdown would teach Module 17 a
    lesson about structure from an accounting event."""
    structural = _classify(target_day=None, stop_day=5, mfe=0.01, realized=-0.30)
    distorted = _classify(
        target_day=None,
        stop_day=5,
        mfe=0.01,
        realized=-0.30,
        actions=(make.split_action(),),
    )

    assert structural.false_positive_type is FalsePositiveType.D_BREAKDOWN
    assert distorted.false_positive_type is FalsePositiveType.G_CORPORATE_ACTION_DISTORTION


def test_every_false_positive_type_is_reachable():
    """The seven are a closed taxonomy; a branch nothing can reach would
    be a category that silently never gets assigned."""
    reached = {
        _classify(**case).false_positive_type
        for case in (
            {"target_day": None, "stop_day": None, "mfe": 0.01, "realized": 0.0},
            {"target_day": None, "stop_day": 10, "mfe": 0.08, "realized": -0.06},
            {"target_day": None, "stop_day": 5, "mfe": 0.01, "realized": -0.30},
            {
                "target_day": None,
                "stop_day": 10,
                "mfe": 0.08,
                "mfe_day": 20,
                "realized": -0.06,
                "events": (make.earnings_event(day=20),),
            },
            {
                "target_day": None,
                "stop_day": 10,
                "mfe": 0.08,
                "realized": -0.06,
                "avg_dollar_volume": 40_000.0,
            },
            {
                "target_day": None,
                "stop_day": 10,
                "mfe": 0.08,
                "realized": -0.06,
                "actions": (make.split_action(),),
            },
        )
    }
    reached.add(FalsePositiveType.A_NO_PATTERN)

    assert reached == set(FalsePositiveType)


def test_no_classification_claims_certainty():
    """None of these heuristics establishes cause, and the confidence
    vocabulary has no value that says otherwise."""
    from core.outcome_tracking.classification import COINCIDENT, INFERRED, WEAK

    results = [
        _classify(target_day=None, stop_day=None, mfe=0.01, realized=0.0),
        _classify(target_day=None, stop_day=10, mfe=0.08, realized=-0.06),
        _classify(
            target_day=None,
            stop_day=10,
            mfe=0.08,
            realized=-0.06,
            actions=(make.split_action(),),
        ),
    ]
    for result in results:
        assert result.false_positive_confidence in {COINCIDENT, INFERRED, WEAK}
