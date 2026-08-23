"""The three narrators, and why they are three.

A live signal, a completed case, and a refusal to score answer different
questions, and the Module 16 brief is explicit that the third is "a
distinct, first-class explanation type — not an error message, not a
degraded version of a normal explanation".

One generator with a mode flag would have produced exactly the degraded
version: the same section list, with "score: unavailable" where a number
should be. So the fact extraction and the verification are shared — that
is where correctness lives — and the narration is three separate
functions with three different section sets. `explain_signal` dispatches
to the refusal narrator on `evidence_status`, because a caller holding a
signal does not know in advance which one it has.

## Nothing here computes

Every number in every sentence arrives through a `Fact.rendered` string.
There is no arithmetic in this file, no comparison that produces a new
value, and no inference beyond selecting which sentence to write. Sentence
*selection* keys on values the input already states — a sufficiency level,
a boolean flag, a status string — which is narration, not analysis.

## Uncertainty is bound to the sentence, not appended to it

The sufficiency of a similarity result changes which sentence is written,
not just the numbers inside it. `INSUFFICIENT` evidence is never narrated
in the register used for `ADEQUATE` evidence, and there is a test that
compares the two texts rather than trusting this paragraph.
"""

from __future__ import annotations

from typing import Any

from core.explanation.facts import (
    FactSet,
    case_facts,
    merge,
    risk_facts,
    signal_facts,
    similarity_facts,
    state_facts,
)
from core.explanation.narrative import (
    CASE,
    INSUFFICIENT_EVIDENCE,
    SIGNAL,
    Claim,
    Explanation,
    Section,
)

SCORED = "SCORED"

#: How a similarity result is spoken about, by its own sufficiency state.
#: The register changes, not just the numbers — which is the difference
#: between reporting uncertainty and burying it.
_SUFFICIENCY_REGISTER: dict[str, str] = {
    "ADEQUATE": (
        "That rests on {sample} comparable historical cases, enough for the "
        "statistics below to be worth reading."
    ),
    "SPARSE": (
        "That rests on only {sample} comparable historical cases — few enough that "
        "the statistics below are indicative at best, and the interval around them "
        "is wide."
    ),
    "INSUFFICIENT": (
        "There are only {sample} comparable historical cases, which is below the "
        "floor at which ARGUS will report outcome statistics at all, so none are "
        "quoted here."
    ),
}


# --------------------------------------------------------------------------
# Live signal
# --------------------------------------------------------------------------


def explain_signal(
    signal: dict[str, Any],
    *,
    similarity: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    state_evidence: dict[str, Any] | None = None,
) -> Explanation:
    """Why ARGUS scored a candidate the way it did — or did not.

    Dispatches to the refusal narrator when the signal is not `SCORED`,
    because a caller holding a signal does not know which it has and
    should not have to branch.
    """
    facts = merge(
        signal_facts(signal),
        similarity_facts(similarity) if similarity else FactSet(),
        risk_facts(risk) if risk else FactSet(),
        state_facts(state_evidence) if state_evidence else FactSet(),
    )
    subject = str(signal.get("security_id", ""))

    if signal.get("evidence_status") != SCORED:
        return _explain_refusal(signal, facts, subject)

    headline = Claim(
        text=(
            # No "out of 100": the score's range is real but the input
            # does not state it, and the verifier rightly refuses a number
            # no fact produced.
            f"ARGUS scored this setup {facts.rendered('signal.argus_score')}, "
            f"with confidence {facts.rendered('signal.confidence')}."
        ),
        cites=("signal.argus_score", "signal.confidence"),
    )

    sections = tuple(
        section
        for section in (
            _pattern_section(facts),
            _evidence_section(facts),
            _risk_section(facts),
            _confidence_section(facts),
            _calibration_section(facts),
        )
        if section is not None and section.claims
    )

    return Explanation(
        kind=SIGNAL,
        subject=subject,
        headline=headline,
        sections=sections,
        facts=facts,
        omissions=_omissions(similarity, risk, state_evidence),
    )


def _pattern_section(facts: FactSet) -> Section | None:
    claims: list[Claim] = []
    if "state.matched" in facts:
        claims.append(
            Claim(
                text=f"The security is in market state {facts.rendered('state.matched')}.",
                cites=("state.matched",),
            )
        )
    if "component.pattern_quality" in facts:
        claims.append(
            Claim(
                text=(
                    "Its match against the target pattern scores "
                    f"{facts.rendered('component.pattern_quality')}, the largest single "
                    "contributor to the composite."
                ),
                cites=("component.pattern_quality",),
            )
        )
    named = [
        (key, facts[key])
        for key in facts.names()
        if key.startswith("component.")
        and not key.endswith(".unmeasured")
        and key != "component.pattern_quality"
    ]
    if named:
        rendered = ", ".join(f"{fact.label} {fact.rendered}" for _key, fact in named)
        claims.append(
            Claim(
                text=f"The remaining components scored: {rendered}.",
                cites=tuple(key for key, _fact in named),
            )
        )
    return Section(name="pattern", claims=tuple(claims))


def _evidence_section(facts: FactSet) -> Section | None:
    """Similarity, with its sufficiency inseparable from its numbers."""
    sufficiency = facts.get("similarity.sufficiency")
    if sufficiency is None:
        return None

    claims: list[Claim] = []
    register = _SUFFICIENCY_REGISTER.get(sufficiency.rendered)
    sample = facts.rendered("similarity.sample_count", "no")
    if register is not None:
        claims.append(
            Claim(
                text=(
                    f"Historical evidence is {sufficiency.rendered}. "
                    + register.format(sample=sample)
                ),
                cites=("similarity.sufficiency", "similarity.sample_count"),
            )
        )

    # Statistics are quoted only when Module 11 reported them, and never
    # without the interval that qualifies them.
    if "similarity.failure_rate" in facts and "similarity.failure_rate_interval" in facts:
        claims.append(
            Claim(
                text=(
                    # The interval's confidence level is deliberately not
                    # stated: Module 11 computes a 95% Wilson interval but
                    # its evidence carries only the bounds, so saying "95%"
                    # here would be this module asserting something its
                    # input does not. Flagged in the module report.
                    f"Among those cases {facts.rendered('similarity.failure_rate')} failed, "
                    f"with an interval of {facts.rendered('similarity.failure_rate_interval')}."
                ),
                cites=("similarity.failure_rate", "similarity.failure_rate_interval"),
            )
        )
    if "similarity.mfe_mean" in facts:
        claims.append(
            Claim(
                text=(
                    f"Their mean favourable excursion was {facts.rendered('similarity.mfe_mean')}."
                ),
                cites=("similarity.mfe_mean",),
            )
        )
    if "similarity.same_asset_backward" in facts:
        claims.append(
            Claim(
                text=(
                    "Separately, this security's own history records "
                    f"{facts.rendered('similarity.same_asset_backward')} retreats along the "
                    "pattern; that is its own record and is not pooled with the cases above."
                ),
                cites=("similarity.same_asset_backward",),
            )
        )
    return Section(name="evidence", claims=tuple(claims))


def _risk_section(facts: FactSet) -> Section | None:
    claims: list[Claim] = []
    raised = [
        (key, facts[key])
        for key in facts.names()
        if key.startswith("risk.") and key.endswith(".raised") and facts[key].value is True
    ]
    if raised:
        claims.append(
            Claim(
                text="Risk flags raised: "
                + ", ".join(fact.rendered for _key, fact in raised)
                + ".",
                cites=tuple(key for key, _fact in raised),
            )
        )

    undetermined = [key for key in facts.names() if key.endswith(".undetermined")]
    if undetermined:
        claims.append(
            Claim(
                text=(
                    "These risk inputs could not be measured and are unknown rather than "
                    "favourable: " + ", ".join(facts[key].rendered for key in undetermined) + "."
                ),
                cites=tuple(undetermined),
            )
        )

    if "risk.next_event_days" in facts and "risk.next_event_type" in facts:
        claims.append(
            Claim(
                text=(
                    f"A scheduled {facts.rendered('risk.next_event_type')} event is "
                    f"{facts.rendered('risk.next_event_days')} days away."
                ),
                cites=("risk.next_event_type", "risk.next_event_days"),
            )
        )
    elif facts.rendered("risk.event_coverage") == "unavailable":
        claims.append(
            Claim(
                text=(
                    "ARGUS had no event-calendar coverage for this security, so whether "
                    "anything is scheduled is unknown."
                ),
                cites=("risk.event_coverage",),
            )
        )

    if facts.get("risk.eligibility_trend") is not None:
        trend = facts["risk.eligibility_trend"]
        if trend.value == "lost_eligibility":
            claims.append(
                Claim(
                    text=(
                        "This security previously passed ARGUS's eligibility gates and no "
                        "longer does."
                    ),
                    cites=("risk.eligibility_trend",),
                )
            )
    return Section(name="risk", claims=tuple(claims))


def _confidence_section(facts: FactSet) -> Section | None:
    """Why confidence is what it is — never a restatement of the score."""
    claims: list[Claim] = []
    if "signal.weight_coverage" in facts:
        claims.append(
            Claim(
                text=(
                    f"{facts.rendered('signal.weight_coverage')} of the score's intended "
                    "inputs could be measured."
                ),
                cites=("signal.weight_coverage",),
            )
        )
    factors = [
        (key, facts[key])
        for key in facts.names()
        if key.startswith("confidence_factor.")
        and not key.endswith(".note")
        and not key.endswith(".sample_size")
    ]
    if factors:
        claims.append(
            Claim(
                text=(
                    "Confidence is assessed separately from the score, from "
                    + ", ".join(f"{fact.label} {fact.rendered}" for _key, fact in factors)
                    + "."
                ),
                cites=tuple(key for key, _fact in factors),
            )
        )
    return Section(name="confidence", claims=tuple(claims))


def _calibration_section(facts: FactSet, *, scored: bool = True) -> Section:
    """The standing caveats. Present on every explanation, both kinds.

    A refusal needs them too, and for a reason that is easy to miss: the
    coverage floor that caused the refusal is itself an unvalidated
    placeholder. A reader told "below the floor" without being told the
    floor is a guess would take the refusal as more principled than it is.
    """
    claims: list[Claim] = []
    if not scored:
        if "signal.calibration_status" in facts:
            claims.append(
                Claim(
                    text=(
                        "The thresholds behind that decision carry status "
                        f"{facts.rendered('signal.calibration_status')} — they have not "
                        "been validated against historical outcomes."
                    ),
                    cites=("signal.calibration_status",),
                )
            )
        return Section(name="calibration", claims=tuple(claims))

    claims.append(
        Claim(
            text=(
                "This score is a composite ranking number, not a probability; ARGUS has no "
                f"calibrated probability for this setup ({facts.rendered('signal.probability_status')})."
            ),
            cites=("signal.probability_status",),
        )
    )
    if "signal.calibration_status" in facts:
        claims.append(
            Claim(
                text=(
                    "The weights behind it carry status "
                    f"{facts.rendered('signal.calibration_status')} — they have not been "
                    "validated against historical outcomes."
                ),
                cites=("signal.calibration_status",),
            )
        )
    return Section(name="calibration", claims=tuple(claims))


# --------------------------------------------------------------------------
# Insufficient evidence — a first-class explanation
# --------------------------------------------------------------------------


def explain_insufficient(
    signal: dict[str, Any],
    *,
    similarity: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    state_evidence: dict[str, Any] | None = None,
) -> Explanation:
    """Why ARGUS declined to score this candidate.

    A complete answer in its own right. "ARGUS could not score this
    because the historical-evidence component was unmeasurable, which
    carries more weight than the coverage floor allows to go missing" is
    more useful than a score would have been, and it is what an honest
    system says most of the time today.
    """
    facts = merge(
        signal_facts(signal),
        similarity_facts(similarity) if similarity else FactSet(),
        risk_facts(risk) if risk else FactSet(),
        state_facts(state_evidence) if state_evidence else FactSet(),
    )
    return _explain_refusal(signal, facts, str(signal.get("security_id", "")))


def _explain_refusal(signal: dict[str, Any], facts: FactSet, subject: str) -> Explanation:
    headline = Claim(
        text=(
            "ARGUS did not score this setup. Its evidence status is "
            f"{facts.rendered('signal.evidence_status')}."
        ),
        cites=("signal.evidence_status",),
    )

    reason: list[Claim] = []
    if "verdict.reason" in facts:
        reason.append(Claim(text=facts.rendered("verdict.reason"), cites=("verdict.reason",)))
    unmeasured = facts.get("verdict.detail.unmeasured_components")
    if unmeasured is not None and unmeasured.value:
        reason.append(
            Claim(
                text=(f"The components that could not be measured were: {unmeasured.rendered}."),
                cites=("verdict.detail.unmeasured_components",),
            )
        )
    undetermined = facts.get("verdict.detail.undetermined_risk_inputs")
    if undetermined is not None and undetermined.value:
        reason.append(
            Claim(
                text=(
                    "The risk inputs that could not be determined were: "
                    f"{undetermined.rendered}. ARGUS treats an unmeasured risk input as "
                    "unknown, never as safe."
                ),
                cites=("verdict.detail.undetermined_risk_inputs",),
            )
        )

    measured: list[Claim] = []
    scored_components = [
        (key, facts[key])
        for key in facts.names()
        if key.startswith("component.") and not key.endswith(".unmeasured")
    ]
    if scored_components:
        measured.append(
            Claim(
                text=(
                    "What ARGUS could measure: "
                    + ", ".join(f"{fact.label} {fact.rendered}" for _key, fact in scored_components)
                    + "."
                ),
                cites=tuple(key for key, _fact in scored_components),
            )
        )
    if "state.matched" in facts:
        measured.append(
            Claim(
                text=f"The security is in market state {facts.rendered('state.matched')}.",
                cites=("state.matched",),
            )
        )

    sections = tuple(
        section
        for section in (
            Section(name="reason", claims=tuple(reason)),
            Section(name="measured", claims=tuple(measured)),
            _evidence_section(facts),
            _risk_section(facts),
            _calibration_section(facts, scored=False),
        )
        if section is not None and section.claims
    )

    return Explanation(
        kind=INSUFFICIENT_EVIDENCE,
        subject=subject,
        headline=headline,
        sections=sections,
        facts=facts,
        omissions=_omissions(None, None, None),
    )


# --------------------------------------------------------------------------
# Case narration
# --------------------------------------------------------------------------


def explain_case(case: dict[str, Any]) -> Explanation:
    """What happened to a completed setup.

    Successes and failures take the identical path — there is no branch on
    classification except where the record itself carries a field only one
    of them has. Module 15 built its case records so a failure is as
    complete as a success; narrating failures more thinly would undo that
    at the last step.
    """
    facts = case_facts(case)
    subject = str(case.get("security_id", ""))

    headline = Claim(
        text=(
            "This setup concluded as "
            f"{facts.rendered('case.classification')} after "
            f"{facts.rendered('case.window_days', '0')} days of tracking."
        )
        if "case.window_days" in facts
        else f"This setup concluded as {facts.rendered('case.classification')}.",
        cites=(
            ("case.classification", "case.window_days")
            if "case.window_days" in facts
            else ("case.classification",)
        ),
    )

    sections = tuple(
        section
        for section in (
            _lifecycle_section(facts),
            _outcome_section(facts),
            _verdict_section(facts),
            _case_context_section(facts),
        )
        if section is not None and section.claims
    )

    return Explanation(
        kind=CASE, subject=subject, headline=headline, sections=sections, facts=facts
    )


def _lifecycle_section(facts: FactSet) -> Section:
    claims: list[Claim] = []
    if "case.stages_reached" in facts:
        claims.append(
            Claim(
                text=(
                    "It moved through these lifecycle stages: "
                    f"{facts.rendered('case.stages_reached')}."
                ),
                cites=("case.stages_reached",),
            )
        )
    retreats = facts.get("case.retreat_count")
    if retreats is not None and retreats.value:
        claims.append(
            Claim(
                text=(
                    f"While active it retreated {retreats.rendered} times along the pattern "
                    "before resolving."
                ),
                cites=("case.retreat_count",),
            )
        )
    if "case.terminal_event" in facts:
        claims.append(
            Claim(
                text=f"Tracking ended because of {facts.rendered('case.terminal_event')}.",
                cites=("case.terminal_event",),
            )
        )
    return Section(name="lifecycle", claims=tuple(claims))


def _outcome_section(facts: FactSet) -> Section:
    claims: list[Claim] = []
    if "case.mfe" in facts and "case.mae" in facts:
        claims.append(
            Claim(
                text=(
                    f"From entry it reached {facts.rendered('case.mfe')} at best and "
                    f"{facts.rendered('case.mae')} at worst."
                ),
                cites=("case.mfe", "case.mae"),
            )
        )
    if "case.realized_return" in facts:
        claims.append(
            Claim(
                text=f"It ended {facts.rendered('case.realized_return')} from entry.",
                cites=("case.realized_return",),
            )
        )
    if "case.benchmark_relative_return" in facts:
        claims.append(
            Claim(
                text=(
                    "Against the benchmark that is "
                    f"{facts.rendered('case.benchmark_relative_return')}."
                ),
                cites=("case.benchmark_relative_return",),
            )
        )
    if "case.window_ends_because" in facts:
        claims.append(
            Claim(
                text=(
                    "The measurement window closed on the "
                    f"{facts.rendered('case.window_ends_because')}."
                ),
                cites=("case.window_ends_because",),
            )
        )
    return Section(name="outcome", claims=tuple(claims))


def _verdict_section(facts: FactSet) -> Section:
    """Why it ended that way, with Module 15's own limits carried across."""
    claims: list[Claim] = []
    if "case.reason" in facts:
        claims.append(Claim(text=facts.rendered("case.reason"), cites=("case.reason",)))

    if "case.false_positive_type" in facts:
        claims.append(
            Claim(
                text=(
                    "ARGUS classified this as false-positive type "
                    f"{facts.rendered('case.false_positive_type')}."
                ),
                cites=("case.false_positive_type",),
            )
        )
        if "case.false_positive_reason" in facts:
            claims.append(
                Claim(
                    text=facts.rendered("case.false_positive_reason"),
                    cites=("case.false_positive_reason",),
                )
            )
        if "case.false_positive_confidence" in facts:
            claims.append(
                Claim(
                    text=(
                        "That classification is "
                        f"{facts.rendered('case.false_positive_confidence')} — a heuristic "
                        "from measurable proxies, not an established cause."
                    ),
                    cites=("case.false_positive_confidence",),
                )
            )
    if "case.review_confidence" in facts:
        claims.append(
            Claim(
                text=(
                    "Confidence in the classification itself is "
                    f"{facts.rendered('case.review_confidence')}, assigned automatically and "
                    "pending human review."
                ),
                cites=("case.review_confidence",),
            )
        )
    return Section(name="verdict", claims=tuple(claims))


def _case_context_section(facts: FactSet) -> Section:
    claims: list[Claim] = []
    if "case.market_regime" in facts:
        claims.append(
            Claim(
                text=(
                    "The market state when tracking ended was "
                    f"{facts.rendered('case.market_regime')}."
                ),
                cites=("case.market_regime",),
            )
        )
    if "case.corporate_actions_in_window" in facts:
        claims.append(
            Claim(
                text=(
                    f"{facts.rendered('case.corporate_actions_in_window')} corporate "
                    "action(s) took effect inside the measurement window."
                ),
                cites=("case.corporate_actions_in_window",),
            )
        )
    if "case.events_in_window" in facts:
        claims.append(
            Claim(
                text=(
                    f"{facts.rendered('case.events_in_window')} scheduled event(s) fell inside it."
                ),
                cites=("case.events_in_window",),
            )
        )
    return Section(name="context", claims=tuple(claims))


def _omissions(
    similarity: dict[str, Any] | None,
    risk: dict[str, Any] | None,
    state_evidence: dict[str, Any] | None,
) -> tuple[str, ...]:
    """Dimensions the caller did not supply.

    Recorded rather than silently skipped: an explanation missing its risk
    section because no risk context was passed should not look like one
    where risk was assessed and found clean.
    """
    absent = []
    if similarity is None:
        absent.append("similarity evidence was not supplied")
    if risk is None:
        absent.append("risk context was not supplied")
    if state_evidence is None:
        absent.append("market-state evidence was not supplied")
    return tuple(absent)
