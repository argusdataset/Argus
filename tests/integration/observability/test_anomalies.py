"""The two anomalies Modules 10 and 17 asked for, against constructed cases."""

from __future__ import annotations

from datetime import timedelta

from core.explanation.facts import FactSet
from core.explanation.narrative import Claim, Explanation, Section
from core.explanation.renderers import DeterministicRenderer, VerifiedRenderer
from infra.db.enums import MarketState
from infra.observability.anomalies import (
    NO_PREDICATE_MATCHED,
    THIN_EVIDENCE,
    observed_renderer,
    state_gaps,
    unclassified_now,
)
from tests.integration.observability.conftest import NOW

# --------------------------------------------------------------------------
# Market state: the gap in the state machine
# --------------------------------------------------------------------------


def test_a_state_machine_gap_is_counted(connection, register, add_transition):
    """Module 10 named it and wrote it down; nothing counted it until now."""
    security_id = register("GAP")
    add_transition(security_id, at=NOW - timedelta(days=2), reason=NO_PREDICATE_MATCHED)

    report = state_gaps(connection, now=NOW)

    assert report.gap_occurrences == 1
    assert report.healthy is False
    assert str(security_id) in report.securities


def test_thin_data_is_counted_separately_and_is_not_a_gap(connection, register, add_transition):
    """The distinction is the whole reason Module 10 split the reason.

    A security ARGUS could not judge is a data problem. A security ARGUS
    could judge and had no state for is a modelling hole. Folding them
    together hides the second permanently.
    """
    thin = register("THIN")
    gap = register("HOLE")
    add_transition(thin, at=NOW - timedelta(days=1), reason=THIN_EVIDENCE)
    add_transition(gap, at=NOW - timedelta(days=1), reason=NO_PREDICATE_MATCHED)

    report = state_gaps(connection, now=NOW)

    assert report.gap_occurrences == 1
    assert report.thin_evidence_occurrences == 1
    assert report.securities == [str(gap)]


def test_the_share_makes_the_count_readable(connection, register, add_transition):
    """Ten gaps against ten thousand thin cases is noise; against twelve it is not."""
    for index in range(9):
        add_transition(register(f"THIN{index}"), at=NOW - timedelta(days=1), reason=THIN_EVIDENCE)
    add_transition(register("HOLE"), at=NOW - timedelta(days=1), reason=NO_PREDICATE_MATCHED)

    report = state_gaps(connection, now=NOW)

    assert report.gap_share == 0.1


def test_no_gaps_is_healthy_and_reports_no_share(connection, register, add_transition):
    """`None`, not `0.0` — nothing was classified, so there is no ratio."""
    report = state_gaps(connection, now=NOW)

    assert report.healthy is True
    assert report.gap_occurrences == 0
    assert report.gap_share is None


def test_a_single_gap_is_enough_to_be_unhealthy(connection, register, add_transition):
    """No tolerance threshold, deliberately.

    Module 10 calls this a gap in the state machine. A threshold here
    would be this module inventing an acceptable amount of a thing the
    module that produces it considers a defect.
    """
    add_transition(register("ONE"), at=NOW - timedelta(hours=1), reason=NO_PREDICATE_MATCHED)

    assert state_gaps(connection, now=NOW).healthy is False


def test_occurrences_outside_the_window_are_not_counted(connection, register, add_transition):
    """So a fixed problem stops being reported."""
    add_transition(register("OLD"), at=NOW - timedelta(days=90), reason=NO_PREDICATE_MATCHED)

    assert state_gaps(connection, now=NOW).gap_occurrences == 0
    assert state_gaps(connection, since=NOW - timedelta(days=365), now=NOW).gap_occurrences == 1


def test_the_window_bounds_report_first_and_last_seen(connection, register, add_transition):
    security_id = register("SPAN")
    first = NOW - timedelta(days=20)
    last = NOW - timedelta(days=2)
    add_transition(security_id, at=first, reason=NO_PREDICATE_MATCHED)
    add_transition(security_id, at=last, reason=NO_PREDICATE_MATCHED)

    report = state_gaps(connection, now=NOW)

    assert report.gap_occurrences == 2
    assert report.first_seen == first
    assert report.last_seen == last


def test_occupancy_is_reported_separately_from_occurrences(
    connection, register, add_transition, set_state
):
    """A security stuck for a month is one occurrence and thirty days of occupancy.

    The transition log records entries, not residence. Reporting one as
    the other is the mistake, so `unclassified_now` answers the second
    question from the projection instead.
    """
    stuck = register("STUCK")
    add_transition(stuck, at=NOW - timedelta(days=25), reason=NO_PREDICATE_MATCHED)
    set_state(stuck, MarketState.UNCLASSIFIED)
    set_state(register("FINE"), MarketState.CONSOLIDATION)

    assert state_gaps(connection, now=NOW).gap_occurrences == 1
    assert unclassified_now(connection) == 1


def test_a_transition_carrying_no_reason_is_ignored(connection, register, add_transition):
    """Ordinary state changes do not carry an `unclassified_reason`."""
    add_transition(
        register("NORMAL"),
        to_state=MarketState.BREAKOUT_READY,
        from_state=MarketState.CONSOLIDATION,
        at=NOW - timedelta(days=1),
    )

    report = state_gaps(connection, now=NOW)

    assert report.gap_occurrences == 0
    assert report.thin_evidence_occurrences == 0


# --------------------------------------------------------------------------
# Explanation: the revert rate
# --------------------------------------------------------------------------


def _explanation() -> Explanation:
    """One explanation with a real fact registry behind it.

    The registry is not decoration: Module 16's verifier rejects a claim
    citing a key it does not hold, so an explanation with an empty
    `FactSet` would have every rephrasing reverted regardless of its
    content — and this file would then be measuring the fixture rather
    than the verifier.
    """
    facts = FactSet()
    facts.put("signal.argus_score", "argus_score", 71.4, "71.4", "ARGUS score")
    facts.put("signal.confidence", "confidence", 0.62, "0.62", "confidence")
    facts.put("signal.risk_score", "risk_score", 44.0, "44.0", "risk score")

    return Explanation(
        kind="signal",
        subject="TEST",
        headline=Claim(text="ARGUS scored this candidate.", cites=("signal.argus_score",)),
        sections=(
            Section(
                name="evidence",
                claims=(
                    Claim(text="The base is well formed.", cites=("signal.confidence",)),
                    Claim(text="Risk is moderate.", cites=("signal.risk_score",)),
                ),
            ),
        ),
        facts=facts,
    )


class _Rewriter:
    """A renderer that rewrites every claim to whatever it is told to."""

    name = "rewriter"

    def __init__(self, replacement) -> None:
        self.replacement = replacement

    def render(self, explanation: Explanation) -> Explanation:
        from dataclasses import replace

        def rewrite(claim: Claim) -> Claim:
            return replace(claim, text=self.replacement(claim.text))

        return replace(
            explanation,
            headline=rewrite(explanation.headline),
            sections=tuple(
                replace(section, claims=tuple(rewrite(claim) for claim in section.claims))
                for section in explanation.sections
            ),
        )


def test_a_renderer_that_proposes_nothing_has_no_revert_rate():
    """`None`, not zero. Silence must not look like success.

    This is precisely the failure Module 17 flagged: a renderer
    contributing nothing produces perfectly correct output, so every
    explanation verifies and the system looks healthy.
    """
    renderer, tally = observed_renderer(DeterministicRenderer())

    renderer.render(_explanation())

    assert tally.proposed == 0
    assert tally.revert_rate is None
    assert tally.healthy is True, "the deterministic renderer doing its job"


def test_a_rephrasing_that_verifies_is_counted_as_kept():
    """A faithful rewording survives, and the tally says so."""
    renderer, tally = observed_renderer(_Rewriter(lambda text: text.replace(".", " overall.")))

    renderer.render(_explanation())

    assert tally.proposed == 3
    assert tally.kept == 3
    assert tally.reverted == 0
    assert tally.revert_rate == 0.0
    assert tally.healthy is True


def test_a_rephrasing_that_introduces_a_number_is_counted_as_reverted():
    """The case that would otherwise look like success.

    Every claim is reverted, the output is perfectly correct, and without
    this counter nothing anywhere would show that the renderer
    contributed exactly nothing.
    """
    renderer, tally = observed_renderer(
        _Rewriter(lambda text: f"{text} The score was 87.3 and rising.")
    )

    renderer.render(_explanation())

    assert tally.proposed == 3
    assert tally.reverted == 3
    assert tally.kept == 0
    assert tally.revert_rate == 1.0
    assert tally.healthy is False


def test_the_tally_accumulates_across_a_run():
    """Module 17 asked for "the revert count per run", not per explanation."""
    renderer, tally = observed_renderer(_Rewriter(lambda text: f"{text} A fabricated 42% figure."))

    for _ in range(4):
        renderer.render(_explanation())

    assert tally.explanations == 4
    assert tally.claims == 12
    assert tally.proposed == 12
    assert tally.reverted == 12


def test_observing_does_not_change_what_a_reader_sees():
    """Which is the property that makes it safe to leave switched on."""
    rewrite = _Rewriter(lambda text: f"{text} A fabricated 42% figure.")
    observed, _tally = observed_renderer(rewrite)

    plain = VerifiedRenderer(inner=rewrite).render(_explanation())
    watched = observed.render(_explanation())

    assert plain.as_dict() == watched.as_dict()


def test_a_mixed_run_separates_kept_from_reverted():
    """The realistic case: some rephrasings survive and some do not."""

    def selectively(text: str) -> str:
        if text.startswith("Risk"):
            return "Risk is moderate, with a 3.2x reward ratio."
        return text.replace(".", ", on the evidence.")

    renderer, tally = observed_renderer(_Rewriter(selectively))

    renderer.render(_explanation())

    assert tally.proposed == 3
    assert tally.kept == 2
    assert tally.reverted == 1
    assert tally.revert_rate == 1 / 3
    assert tally.healthy is True, "some contribution is getting through"


def test_the_tally_serialises_for_a_run_report():
    renderer, tally = observed_renderer(DeterministicRenderer())
    renderer.render(_explanation())

    payload = tally.as_dict()

    assert payload["revert_rate"] is None
    assert payload["explanations"] == 1
    assert set(payload) == {
        "explanations",
        "claims",
        "proposed",
        "kept",
        "reverted",
        "revert_rate",
        "healthy",
    }
