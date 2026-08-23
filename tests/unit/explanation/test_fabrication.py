"""The fabrication test: nothing in the prose that is not in the input.

This module's equivalent of Module 07's leakage query and Module 15's PIT
excursion test. The failure it exists to catch is linguistic rather than
arithmetic — a fabricated number reads exactly like a real one, and no
reviewer scanning prose would reliably spot "84 historical analogues" when
the input said six.

The check is set membership, in both directions:

* **Everything in the text traces to a field.** Every numeric token and
  every upper-case domain term in the finished prose must appear in some
  fact's rendered string, and every fact records the input path it came
  from.
* **Nothing in the text lacks a field.** Every claim cites at least one
  fact, enforced at construction and re-checked by the verifier, with no
  uncited-connective escape hatch to hide an unsupported sentence in.
"""

from __future__ import annotations

import re

import pytest

from core.explanation import (
    UNCITED_CLAIM,
    UNKNOWN_FACT,
    UNLICENSED_NUMBER,
    UNLICENSED_TERM,
    Claim,
    Fabrication,
    assert_faithful,
    explain_case,
    explain_signal,
    unused_facts,
    verify,
)
from core.explanation.narrative import Section
from tests.unit.explanation import factories as make

_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?")


def _scenarios():
    return {
        "scored_high_confidence": explain_signal(**make.signal_bundle()),
        "high_score_low_confidence": explain_signal(**make.high_score_low_confidence()),
        "insufficient_evidence": explain_signal(**make.refused()),
        "failed_case": explain_case(**make.failed_case()),
        "successful_case": explain_case(**make.successful_case()),
    }


SCENARIOS = _scenarios()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_explanation_contains_anything_its_input_did_not(name):
    """The headline assertion, across every explanation type the brief
    names — a fully-scored case, Module 13's high-score/low-confidence
    example, an INSUFFICIENT_EVIDENCE refusal, and a completed FAILED case
    with a false-positive classification."""
    assert verify(SCENARIOS[name]) == ()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_claim_cites_at_least_one_fact(name):
    """The "nothing exists that isn't in the input" half. There is no
    category of sentence exempt from citing — connective wording lives
    inside a claim's template, attached to the facts licensing the rest."""
    for claim in SCENARIOS[name].claims():
        assert claim.cites, claim.text


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_citation_resolves_to_a_real_fact(name):
    explanation = SCENARIOS[name]
    for claim in explanation.claims():
        for key in claim.cites:
            assert key in explanation.facts, f"{key} in {claim.text!r}"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_number_in_the_prose_came_from_a_field(name):
    """Checked here independently of the verifier, so a bug in the
    verifier cannot make this test vacuous."""
    explanation = SCENARIOS[name]
    licensed = explanation.facts.licensed_tokens()

    for number in _NUMBER.findall(explanation.text()):
        assert number in licensed, f"{number!r} appears in prose but in no field"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_fact_records_where_it_came_from(name):
    """A number in the text is only checkable if the fact behind it names
    its own path in the input."""
    for key, entry in SCENARIOS[name].facts.as_dict().items():
        assert entry["path"], key


# --------------------------------------------------------------------------
# The verifier actually catches fabrication
# --------------------------------------------------------------------------


def _tamper(explanation, text: str, cites=None):
    """Replace the headline with a claim someone might plausibly write."""
    from dataclasses import replace

    return replace(
        explanation,
        headline=Claim(text=text, cites=cites or explanation.headline.cites),
    )


def test_a_fabricated_number_is_caught():
    """The canonical failure: a plausible statistic that no field holds.

    The calibration example from the project's early planning is "84
    historical analogues ... 71% positive outcome rate". Written against
    an input that says six cases, both numbers are inventions, and both
    are caught.
    """
    explanation = SCENARIOS["high_score_low_confidence"]
    tampered = _tamper(
        explanation,
        "ARGUS found 84 historical analogues with a 71% positive outcome rate.",
    )

    violations = verify(tampered)
    offenders = {violation.offender for violation in violations}

    assert {"84", "71%"} <= offenders
    assert all(violation.kind == UNLICENSED_NUMBER for violation in violations)


def test_a_fabricated_domain_term_is_caught():
    """Narrating an INSUFFICIENT result as ADEQUATE is the exact
    misrepresentation the brief singles out, and it is a term-level
    fabrication rather than a numeric one."""
    explanation = explain_signal(**make.signal_bundle(cross=make.sparse()))
    tampered = _tamper(
        explanation, "Historical evidence is ADEQUATE.", cites=("similarity.sufficiency",)
    )

    violations = verify(tampered)

    assert any(
        violation.kind == UNLICENSED_TERM and violation.offender == "ADEQUATE"
        for violation in violations
    )


def test_a_claim_citing_a_fact_that_does_not_exist_is_caught():
    from dataclasses import replace

    explanation = SCENARIOS["scored_high_confidence"]
    tampered = replace(
        explanation,
        headline=Claim(text="Something happened.", cites=("signal.invented_field",)),
    )

    violations = verify(tampered)

    assert any(
        violation.kind == UNKNOWN_FACT and violation.offender == "signal.invented_field"
        for violation in violations
    )


def test_an_uncited_claim_cannot_even_be_constructed():
    """Enforced at construction, so the common case never reaches the
    verifier at all."""
    from core.explanation import UncitedClaim

    with pytest.raises(UncitedClaim):
        Claim(text="ARGUS is fairly confident about this one.", cites=())


def test_an_uncited_claim_smuggled_past_the_constructor_is_still_caught():
    """Belt and braces: an explanation can arrive from a renderer or a
    deserialized payload, so the verifier re-checks what the constructor
    already enforced."""
    from dataclasses import replace

    explanation = SCENARIOS["scored_high_confidence"]
    smuggled = Claim(text="Looks good.", cites=("signal.argus_score",))
    object.__setattr__(smuggled, "cites", ())
    tampered = replace(explanation, sections=(Section(name="x", claims=(smuggled,)),))

    assert any(violation.kind == UNCITED_CLAIM for violation in verify(tampered))


def test_assert_faithful_raises_on_a_fabrication():
    tampered = _tamper(SCENARIOS["scored_high_confidence"], "There were 999 analogues.")

    with pytest.raises(Fabrication, match="999"):
        assert_faithful(tampered)

    assert assert_faithful(SCENARIOS["scored_high_confidence"]) is not None


# --------------------------------------------------------------------------
# Ablation: a removed input is never replaced by an invention
# --------------------------------------------------------------------------


def test_stripping_similarity_removes_the_section_rather_than_inventing_one():
    """The brief's third requirement, and the one a plausible-sounding
    generator would fail: with no similarity evidence at all, the
    explanation must omit that dimension or say it is unavailable — never
    substitute something that reads right."""
    with_evidence = explain_signal(**make.signal_bundle())
    without = explain_signal(**make.signal_bundle(with_similarity=False))

    assert with_evidence.section("evidence") is not None
    assert without.section("evidence") is None
    assert verify(without) == ()
    assert "similarity evidence was not supplied" in without.omissions
    # And no analogue count, sufficiency word, or failure rate leaked in.
    for token in ("ADEQUATE", "SPARSE", "INSUFFICIENT", "analogue", "comparable historical"):
        assert token not in without.text()


@pytest.mark.parametrize(
    ("dimension", "section"),
    [("with_risk", "risk"), ("with_state", "pattern")],
)
def test_stripping_any_input_dimension_never_fabricates_a_replacement(dimension, section):
    stripped = explain_signal(**make.signal_bundle(**{dimension: False}))

    assert verify(stripped) == ()
    if section == "risk":
        assert stripped.section("risk") is None
        assert "risk context was not supplied" in stripped.omissions


def test_an_explanation_built_from_nothing_but_a_signal_still_verifies():
    """The minimal input. It should be thin, not wrong."""
    bare = explain_signal(
        **make.signal_bundle(with_similarity=False, with_risk=False, with_state=False)
    )

    assert verify(bare) == ()
    assert len(bare.omissions) == 3
    assert bare.text()


def test_unused_facts_are_reported_rather_than_hidden():
    """Not a violation — an explanation summarises. Reported because a
    systematically unused fact usually means a narrator forgot a
    dimension, which is invisible otherwise."""
    unused = unused_facts(SCENARIOS["scored_high_confidence"])

    assert isinstance(unused, tuple)
    assert all(key in SCENARIOS["scored_high_confidence"].facts for key in unused)
