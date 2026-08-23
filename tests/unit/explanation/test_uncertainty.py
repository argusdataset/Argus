"""Honest uncertainty survives into the prose, rather than being smoothed away.

The Module 16 brief names this as a failure mode in its own right:
"'ARGUS is fairly confident' when the actual `confidence` value is 33.7
and the evidence is thin is a failure of this module, not a stylistic
choice." These tests read the generated text, not the underlying numbers.
"""

from __future__ import annotations

import pytest

from core.explanation import explain_signal, verify
from tests.unit.explanation import factories as make
from tests.unit.scoring.factories import adequate, insufficient, sparse


def _text(**kwargs) -> str:
    return explain_signal(**make.signal_bundle(**kwargs)).text()


# --------------------------------------------------------------------------
# Sufficiency changes the register, not just the numbers
# --------------------------------------------------------------------------


def test_sparse_and_adequate_evidence_are_narrated_differently():
    """The brief's requirement: "demonstrably different in their expressed
    certainty, not just their numbers".

    Both inputs carry the same 30% failure rate. What differs is 200 cases
    against 6 — and the sentences say so in words, not only in digits.
    """
    strong = explain_signal(**make.signal_bundle(cross=adequate(0.30)))
    thin = explain_signal(**make.signal_bundle(cross=sparse(0.30)))

    strong_text = strong.section("evidence").text()
    thin_text = thin.section("evidence").text()

    assert "ADEQUATE" in strong_text
    assert "SPARSE" in thin_text
    assert "worth reading" in strong_text
    assert "indicative at best" in thin_text
    # Same point estimate, different register — so the difference is not
    # just the numbers being different.
    assert "30.0%" in strong_text and "30.0%" in thin_text


def test_insufficient_evidence_quotes_no_statistics_at_all():
    """Module 11 reports none below its floor, and narrating a statistic
    that does not exist would be the purest form of fabrication."""
    explanation = explain_signal(**make.signal_bundle(cross=insufficient()))
    evidence = explanation.section("evidence")

    assert evidence is not None
    assert "INSUFFICIENT" in evidence.text()
    assert "below the floor" in evidence.text()
    assert "failed" not in evidence.text()
    assert "interval" not in evidence.text()


def test_a_statistic_is_never_quoted_without_its_interval():
    """A failure rate on its own is the number a reader will remember, and
    the interval is what says how much to trust it."""
    text = _text(cross=sparse(0.30))

    assert "30.0%" in text
    assert "with an interval of" in text


def test_the_interval_confidence_level_is_not_asserted():
    """Module 11 computes a 95% Wilson interval, but its evidence carries
    only the bounds. Saying "95%" here would be this module asserting
    something its input does not contain — the verifier catches it, and
    this records that the omission is deliberate rather than an oversight.
    """
    explanation = explain_signal(**make.signal_bundle(cross=sparse()))

    assert "95%" not in explanation.text()
    assert verify(explanation) == ()


# --------------------------------------------------------------------------
# High score, low confidence
# --------------------------------------------------------------------------


def test_thin_evidence_is_legible_in_the_text_not_just_the_numbers():
    """Module 13's worked example: everything structural is excellent, the
    evidence behind it is six cases and a 60%-filled feature window. A
    reader of the prose alone must be able to tell."""
    bundle = make.high_score_low_confidence()
    explanation = explain_signal(**bundle)
    text = explanation.text()

    assert bundle["signal"]["argus_score"] > 80.0
    assert bundle["signal"]["confidence"] < 40.0

    # The score and the confidence are both stated, adjacent, unrounded.
    assert "87.7" in text and "33.7" in text
    # And the reason confidence is low is spelled out, not left implicit.
    assert "SPARSE" in text
    assert "indicative at best" in text


def test_nothing_reassuring_is_added_to_a_thin_case():
    """No smoothing. The generator has no vocabulary for confidence that
    is not bound to a field."""
    text = make.high_score_low_confidence() and _text(
        cross=sparse(0.05), quality=1.0, feature_coverage=0.6
    )

    for softener in ("fairly confident", "strong evidence", "likely", "should", "expect"):
        assert softener not in text.lower()


# --------------------------------------------------------------------------
# Probability is never implied
# --------------------------------------------------------------------------


def test_no_probability_figure_appears_when_none_is_calibrated():
    """`probability` is unpopulated everywhere in ARGUS today. An
    explanation that implied one would misrepresent the single number the
    project has been most careful about."""
    explanation = explain_signal(**make.signal_bundle())
    text = explanation.text()

    assert explanation.facts.get("signal.probability_status") is not None
    assert "signal.probability" not in explanation.facts
    assert "chance" not in text.lower()
    assert "probability of" not in text.lower()
    assert "NOT_YET_CALIBRATED" in text


def test_the_score_is_explicitly_said_not_to_be_a_probability():
    text = _text()

    assert "not a probability" in text


@pytest.mark.parametrize("scenario", ["signal", "refused"])
def test_the_calibration_status_of_the_weights_is_stated(scenario):
    explanation = explain_signal(
        **(make.signal_bundle() if scenario == "signal" else make.refused())
    )

    assert "UNVALIDATED_PLACEHOLDERS" in explanation.text()


# --------------------------------------------------------------------------
# Undetermined risk is never silence
# --------------------------------------------------------------------------


def test_undetermined_risk_inputs_are_named_rather_than_omitted():
    """Module 12's binding finding: an unmeasured risk flag is unknown,
    not favourable. An explanation that dropped it would undo that at the
    last step."""
    explanation = explain_signal(**make.signal_bundle(cross=adequate(), liquidity=None))
    risk = explanation.section("risk")

    assert risk is not None
    assert "could not be measured" in risk.text()
    assert "unknown rather than" in risk.text()
    assert "liquidity degree" in risk.text()


def test_missing_event_coverage_is_stated_not_read_as_no_events():
    from core.risk_context.events import EventCoverage

    explanation = explain_signal(
        **make.signal_bundle(
            cross=adequate(), event_coverage=EventCoverage.UNAVAILABLE, days_to_event=None
        )
    )

    assert "no event-calendar coverage" in explanation.text()


# --------------------------------------------------------------------------
# The refusal is a real explanation
# --------------------------------------------------------------------------


def test_a_refusal_cites_the_actual_blocking_reason():
    """Not a generic "not enough data" — the specific component that could
    not be measured, quoted from Module 13's verdict."""
    explanation = explain_signal(**make.refused())
    text = explanation.text()

    assert explanation.kind == "insufficient_evidence"
    assert "historical evidence" in text
    assert "component weight could be measured" in text
    assert "not enough data" not in text.lower()


def test_a_refusal_also_says_what_it_could_measure():
    """A first-class explanation, not a degraded one. Knowing the pattern
    matched well and only the historical evidence was missing is more
    useful than a score would have been."""
    explanation = explain_signal(**make.refused())
    measured = explanation.section("measured")

    assert measured is not None
    assert "What ARGUS could measure" in measured.text()
    assert "pattern quality" in measured.text()


def test_a_refusal_never_states_a_score():
    explanation = explain_signal(**make.refused())

    assert "signal.argus_score" not in explanation.facts
    assert "scored this setup" not in explanation.text()
