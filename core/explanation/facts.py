"""The input surface, and the registry every sentence must draw from.

## Why a registry at all

This module narrates; it does not analyse. The difference has to be
structural rather than intended, because the failure mode here is
linguistic: a fabricated number reads exactly like a real one, and no
amount of careful prompting makes "84 historical analogues" verifiable
when the input said six.

So text is never written directly. Every value that can appear in an
explanation is first extracted into a `Fact` — a key, the path it came
from, the raw value, and **the exact string it is allowed to contribute
to prose**. A claim then cites the facts it rests on, and
`verification.py` checks the finished text against the registry: every
number in the prose must be a number some fact rendered, and every claim
must cite something.

That turns "did the model make this up" from a judgement into a set
membership test.

## The input surface is plain dictionaries, on purpose

Everything here takes `dict` — the `as_dict()` output of Module 13's
signal, Module 15's case record, Module 12's risk context, Module 10's
state evidence, and an adapter for Module 11's similarity object. Nothing
in this package takes a `Connection`, imports a table, or imports
SQLAlchemy at all, which is what makes "cannot read outside the input
surface" a fact about the code rather than a promise. There is a test
that fails if a database import ever appears here.

The one adapter — `similarity_as_dict` — exists because Module 11 is the
only one of the five that never grew an `as_dict()`. Flagged in the
module report; it reads public attributes and nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: The five structured inputs this module may read, and nothing else.
#: Named so the boundary is greppable and so the enforcement test has
#: something to assert against.
INPUT_SURFACE: tuple[str, ...] = (
    "module_13_signal",
    "module_15_case_record",
    "module_12_risk_context",
    "module_11_similarity",
    "module_10_state_evidence",
)

#: Numeric tokens that may appear in prose without a fact behind them.
#: Deliberately tiny: these are the only numbers a sentence can contain
#: for grammatical rather than factual reasons.
STRUCTURAL_NUMBERS: frozenset[str] = frozenset({"0", "1", "2"})

_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?")
#: Domain vocabulary is upper case throughout ARGUS — states, statuses,
#: sufficiency levels, false-positive types. Any such token in prose must
#: come from a fact, which is what stops an `INSUFFICIENT` result being
#: narrated as `ADEQUATE`.
_TERM = re.compile(r"\b[A-Z][A-Z_]{2,}\b")


@dataclass(frozen=True, slots=True)
class Fact:
    """One value from the input, and the exact text it may contribute."""

    key: str
    #: Where in the input it came from, dotted. Carried so a reader of the
    #: explanation can find the field rather than trust the sentence.
    path: str
    value: Any
    #: The only string form of this value allowed in prose.
    rendered: str
    label: str

    def tokens(self) -> set[str]:
        """Numeric and domain tokens this fact licenses."""
        return set(_NUMBER.findall(self.rendered)) | set(_TERM.findall(self.rendered))


@dataclass(frozen=True, slots=True)
class FactSet:
    """Every fact one explanation may draw on."""

    facts: dict[str, Fact] = field(default_factory=dict)

    def add(self, fact: Fact) -> FactSet:
        self.facts[fact.key] = fact
        return self

    def put(self, key: str, path: str, value: Any, rendered: str, label: str) -> FactSet:
        return self.add(Fact(key=key, path=path, value=value, rendered=rendered, label=label))

    def __contains__(self, key: str) -> bool:
        return key in self.facts

    def __getitem__(self, key: str) -> Fact:
        return self.facts[key]

    def get(self, key: str) -> Fact | None:
        return self.facts.get(key)

    def rendered(self, key: str, default: str = "") -> str:
        fact = self.facts.get(key)
        return default if fact is None else fact.rendered

    def names(self) -> tuple[str, ...]:
        return tuple(self.facts)

    def licensed_tokens(self) -> set[str]:
        """Every token any fact permits, plus the structural numbers."""
        allowed = set(STRUCTURAL_NUMBERS)
        for fact in self.facts.values():
            allowed |= fact.tokens()
        return allowed

    def as_dict(self) -> dict[str, Any]:
        return {
            key: {"path": fact.path, "value": fact.value, "rendered": fact.rendered}
            for key, fact in self.facts.items()
        }


# --------------------------------------------------------------------------
# Rendering helpers — the only places a value becomes a string
# --------------------------------------------------------------------------


def score(value: float) -> str:
    """A 0-100 score, one decimal. Never rounded to look tidier."""
    return f"{value:.1f}"


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def ratio(value: float) -> str:
    return f"{value:.2f}"


def count(value: int) -> str:
    return str(int(value))


# --------------------------------------------------------------------------
# Module 13 — the scored signal
# --------------------------------------------------------------------------


def signal_facts(signal: dict[str, Any]) -> FactSet:
    """Facts from Module 13's `ScoredSignal.as_dict()` or a `signals` row."""
    facts = FactSet()
    facts.put(
        "signal.evidence_status",
        "evidence_status",
        signal.get("evidence_status"),
        str(signal.get("evidence_status")),
        "evidence status",
    )
    facts.put(
        "signal.decision",
        "decision",
        signal.get("decision"),
        str(signal.get("decision")),
        "decision",
    )

    for name, label in (
        ("argus_score", "ARGUS score"),
        ("confidence", "confidence"),
        ("opportunity_score", "opportunity score"),
        ("risk_score", "risk score"),
    ):
        value = signal.get(name)
        if value is not None:
            facts.put(f"signal.{name}", name, value, score(float(value)), label)

    # `probability` is deliberately absent as a *number*. Its status is a
    # fact, so an explanation can say the model is not calibrated — which
    # is a claim about the system, not a forecast.
    facts.put(
        "signal.probability_status",
        "probability_status",
        signal.get("probability_status"),
        str(signal.get("probability_status", "")),
        "probability status",
    )
    facts.put(
        "signal.probability_definition",
        "probability_definition",
        signal.get("probability_definition"),
        str(signal.get("probability_definition", "")),
        "probability definition",
    )

    coverage = signal.get("weight_coverage")
    if coverage is not None:
        facts.put(
            "signal.weight_coverage",
            "weight_coverage",
            coverage,
            percent(float(coverage)),
            "share of the score that could be measured",
        )

    for name, component in (signal.get("components") or {}).items():
        value = component.get("value")
        if value is not None:
            facts.put(
                f"component.{name}",
                f"components.{name}.value",
                value,
                score(float(value)),
                name.replace("_", " "),
            )
        elif component.get("weight", 0.0) > 0:
            facts.put(
                f"component.{name}.unmeasured",
                f"components.{name}.value",
                None,
                name.replace("_", " "),
                f"{name} could not be measured",
            )

    assessment = signal.get("confidence_assessment") or {}
    for name, factor in (assessment.get("factors") or {}).items():
        value = factor.get("value")
        if value is not None:
            facts.put(
                f"confidence_factor.{name}",
                f"confidence_assessment.factors.{name}.value",
                value,
                score(float(value)),
                name.replace("_", " "),
            )
        note = factor.get("note")
        if note and name == "sample_sufficiency":
            facts.put(
                "confidence_factor.sample_sufficiency.note",
                "confidence_assessment.factors.sample_sufficiency.note",
                note,
                str(note),
                "analogue sample sufficiency",
            )
        reading = factor.get("reading")
        if reading is not None and name == "sample_sufficiency":
            facts.put(
                "confidence_factor.sample_size",
                "confidence_assessment.factors.sample_sufficiency.reading",
                reading,
                count(reading),
                "analogues found",
            )

    verdict = signal.get("verdict") or {}
    if verdict.get("reason"):
        facts.put(
            "verdict.reason", "verdict.reason", verdict["reason"], str(verdict["reason"]), "reason"
        )
    for name, value in (verdict.get("detail") or {}).items():
        facts.put(
            f"verdict.detail.{name}",
            f"verdict.detail.{name}",
            value,
            _render_detail(value),
            name.replace("_", " "),
        )

    facts.put(
        "signal.calibration_status",
        "calibration_status",
        signal.get("calibration_status"),
        str(signal.get("calibration_status", "")),
        "calibration status",
    )
    return facts


# --------------------------------------------------------------------------
# Module 11 — similarity evidence
# --------------------------------------------------------------------------


def similarity_as_dict(evidence: Any) -> dict[str, Any]:
    """Module 11's `SimilarityEvidence` as a plain dict.

    The only adapter in this module, because Module 11 is the one input of
    the five that never grew an `as_dict()`. Reads public attributes and
    computes nothing — the sufficiency state and the interval come across
    intact, which is the whole point.
    """

    def scoped(scope: Any) -> dict[str, Any]:
        statistics = scope.statistics
        interval = statistics.failure_rate_interval
        return {
            "scope": scope.scope.value,
            "match_count": scope.count,
            "considered": scope.considered,
            "sufficiency": statistics.sufficiency.value,
            "sample_count": statistics.sample_count,
            "failure_rate": statistics.failure_rate,
            "failure_rate_interval": (
                None if interval is None else {"low": interval.low, "high": interval.high}
            ),
            "median_outcome": statistics.median_outcome,
            "mfe_mean": None if statistics.mfe is None else statistics.mfe.mean,
        }

    return {
        "cross_asset": scoped(evidence.cross_asset),
        "same_asset": scoped(evidence.same_asset),
        "same_asset_history": evidence.same_asset_history.as_dict(),
    }


def similarity_facts(similarity: dict[str, Any]) -> FactSet:
    """Facts from Module 11's evidence, sufficiency never separated from
    the numbers it qualifies."""
    facts = FactSet()
    cross = similarity.get("cross_asset") or {}

    sufficiency = cross.get("sufficiency")
    if sufficiency:
        facts.put(
            "similarity.sufficiency",
            "cross_asset.sufficiency",
            sufficiency,
            str(sufficiency),
            "sample sufficiency",
        )
    sample = cross.get("sample_count")
    if sample is not None:
        facts.put(
            "similarity.sample_count",
            "cross_asset.sample_count",
            sample,
            count(sample),
            "comparable historical cases",
        )

    rate = cross.get("failure_rate")
    if rate is not None:
        facts.put(
            "similarity.failure_rate",
            "cross_asset.failure_rate",
            rate,
            percent(float(rate)),
            "failure rate among analogues",
        )
    interval = cross.get("failure_rate_interval")
    if interval:
        facts.put(
            "similarity.failure_rate_interval",
            "cross_asset.failure_rate_interval",
            interval,
            f"{percent(float(interval['low']))} to {percent(float(interval['high']))}",
            "95% interval on the failure rate",
        )
    median = cross.get("median_outcome")
    if median is not None:
        facts.put(
            "similarity.median_outcome",
            "cross_asset.median_outcome",
            median,
            percent(float(median)),
            "median outcome among analogues",
        )
    mfe = cross.get("mfe_mean")
    if mfe is not None:
        facts.put(
            "similarity.mfe_mean",
            "cross_asset.mfe_mean",
            mfe,
            percent(float(mfe)),
            "mean favourable excursion among analogues",
        )

    history = similarity.get("same_asset_history") or {}
    backward = history.get("backward_transitions")
    if backward is not None:
        facts.put(
            "similarity.same_asset_backward",
            "same_asset_history.backward_transitions",
            backward,
            count(backward),
            "this security's own retreats",
        )
    return facts


# --------------------------------------------------------------------------
# Module 12 — risk context
# --------------------------------------------------------------------------


def risk_facts(risk: dict[str, Any]) -> FactSet:
    """Facts from Module 12's `RiskContext.as_dict()`.

    An undetermined flag becomes a fact about *not knowing*, never an
    absence. Module 12's whole design is that unmeasured risk is not zero
    risk, and an explanation that silently omitted the flag would undo
    that at the last step.
    """
    facts = FactSet()
    for name, flag in (risk.get("flags") or {}).items():
        raised = flag.get("raised")
        if raised is None:
            facts.put(
                f"risk.{name}.undetermined",
                f"flags.{name}.unavailable",
                flag.get("unavailable"),
                name.replace("_", " "),
                f"{name} could not be determined",
            )
            continue
        facts.put(
            f"risk.{name}.raised",
            f"flags.{name}.raised",
            raised,
            name.replace("_", " "),
            f"{name} flag",
        )
        degree = flag.get("degree")
        if degree is not None:
            facts.put(
                f"risk.{name}.degree",
                f"flags.{name}.degree",
                degree,
                ratio(float(degree)),
                f"{name} reading",
            )

    events = risk.get("events") or {}
    coverage = events.get("coverage")
    if coverage:
        facts.put(
            "risk.event_coverage",
            "events.coverage",
            coverage,
            str(coverage),
            "event calendar coverage",
        )
    upcoming = events.get("events") or []
    if upcoming:
        soonest = upcoming[0]
        facts.put(
            "risk.next_event_type",
            "events.events.0.event_type",
            soonest.get("event_type"),
            str(soonest.get("event_type")),
            "next scheduled event",
        )
        days = soonest.get("days_until")
        if days is not None:
            facts.put(
                "risk.next_event_days",
                "events.events.0.days_until",
                days,
                f"{float(days):.1f}",
                "days until the next scheduled event",
            )

    invalidation = risk.get("invalidation") or {}
    backward = invalidation.get("backward_transitions")
    if backward is not None:
        facts.put(
            "risk.backward_transitions",
            "invalidation.backward_transitions",
            backward,
            count(backward),
            "recorded retreats",
        )
    trend = (invalidation.get("eligibility") or {}).get("trend")
    if trend:
        facts.put(
            "risk.eligibility_trend",
            "invalidation.eligibility.trend",
            trend,
            str(trend),
            "eligibility trend",
        )

    for name, reason in (risk.get("missing_inputs") or {}).items():
        facts.put(
            f"risk.missing.{name}",
            f"missing_inputs.{name}",
            reason,
            name.replace("_", " "),
            f"{name} was unavailable",
        )
    return facts


# --------------------------------------------------------------------------
# Module 10 — state evidence
# --------------------------------------------------------------------------


def state_facts(evidence: dict[str, Any]) -> FactSet:
    """Facts from Module 10's assignment evidence JSONB.

    The target model's `quality` is available as a fact; Module 10's state
    `confidence` is deliberately not extracted, matching every downstream
    module's refusal to treat it as evidence.
    """
    facts = FactSet()
    matched = evidence.get("matched_state")
    if matched:
        facts.put("state.matched", "matched_state", matched, str(matched), "market state")

    assessment = evidence.get("target_model_assessment") or {}
    if assessment.get("assessed") and assessment.get("quality") is not None:
        facts.put(
            "state.pattern_quality",
            "target_model_assessment.quality",
            assessment["quality"],
            ratio(float(assessment["quality"])),
            "target-model pattern match",
        )
    for name, value in (assessment.get("components") or {}).items():
        facts.put(
            f"state.component.{name}",
            f"target_model_assessment.components.{name}",
            value,
            ratio(float(value)),
            name.replace("_", " "),
        )
    for name in assessment.get("unavailable_inputs") or ():
        facts.put(
            f"state.unavailable.{name}",
            "target_model_assessment.unavailable_inputs",
            name,
            name.replace("_", " "),
            f"{name} was unavailable to the target model",
        )
    return facts


# --------------------------------------------------------------------------
# Module 15 — the case record
# --------------------------------------------------------------------------


def case_facts(case: dict[str, Any]) -> FactSet:
    """Facts from Module 15's `CaseRecord.as_dict()`.

    Successes and failures go through this function identically — there is
    no branch on classification anywhere in it, which is how a failure
    narration ends up with the same evidence available to it as a success.
    """
    facts = FactSet()
    facts.put(
        "case.classification",
        "classification",
        case.get("classification"),
        str(case.get("classification")),
        "outcome",
    )

    stages = case.get("stages") or {}
    for name in ("detected_at", "qualified_at", "activated_at", "concluded_at"):
        value = stages.get(name)
        if value:
            facts.put(
                f"case.{name}", f"stages.{name}", value, str(value)[:10], name.replace("_", " ")
            )
    reached = stages.get("stages_reached") or []
    if reached:
        facts.put(
            "case.stages_reached",
            "stages.stages_reached",
            reached,
            ", ".join(str(stage) for stage in reached),
            "lifecycle stages reached",
        )
    retreats = stages.get("retreat_count")
    if retreats is not None:
        facts.put(
            "case.retreat_count",
            "stages.retreat_count",
            retreats,
            count(retreats),
            "retreats recorded",
        )
    terminal = stages.get("terminal_event_type")
    if terminal:
        facts.put(
            "case.terminal_event",
            "stages.terminal_event_type",
            terminal,
            str(terminal).replace("_", " "),
            "how tracking ended",
        )

    outcome = case.get("outcome") or {}
    for name, label, render in (
        ("mfe", "peak favourable excursion", percent),
        ("mae", "peak adverse excursion", percent),
        ("realized_return", "realized return", percent),
        ("benchmark_relative_return", "return against the benchmark", percent),
        ("volatility_adjusted_outcome", "volatility-adjusted outcome", ratio),
    ):
        value = outcome.get(name)
        if value is not None:
            facts.put(f"case.{name}", f"outcome.{name}", value, render(float(value)), label)
    window = outcome.get("window") or {}
    if window.get("duration_days") is not None:
        facts.put(
            "case.window_days",
            "outcome.window.duration_days",
            window["duration_days"],
            f"{float(window['duration_days']):.0f}",
            "days tracked",
        )
    if window.get("ends_because"):
        facts.put(
            "case.window_ends_because",
            "outcome.window.ends_because",
            window["ends_because"],
            str(window["ends_because"]).replace("_", " "),
            "why the window closed",
        )

    verdict = case.get("verdict") or {}
    if verdict.get("reason"):
        facts.put(
            "case.reason", "verdict.reason", verdict["reason"], str(verdict["reason"]), "reason"
        )
    if verdict.get("false_positive_type"):
        facts.put(
            "case.false_positive_type",
            "verdict.false_positive_type",
            verdict["false_positive_type"],
            str(verdict["false_positive_type"]),
            "false-positive type",
        )
    if verdict.get("false_positive_reason"):
        facts.put(
            "case.false_positive_reason",
            "verdict.false_positive_reason",
            verdict["false_positive_reason"],
            str(verdict["false_positive_reason"]),
            "why it was classified that way",
        )
    if verdict.get("false_positive_confidence"):
        facts.put(
            "case.false_positive_confidence",
            "verdict.false_positive_confidence",
            verdict["false_positive_confidence"],
            str(verdict["false_positive_confidence"]),
            "confidence in that classification",
        )
    if verdict.get("review_confidence"):
        facts.put(
            "case.review_confidence",
            "verdict.review_confidence",
            verdict["review_confidence"],
            str(verdict["review_confidence"]),
            "review confidence",
        )

    context = case.get("context") or {}
    if context.get("market_regime_at_outcome"):
        facts.put(
            "case.market_regime",
            "context.market_regime_at_outcome",
            context["market_regime_at_outcome"],
            str(context["market_regime_at_outcome"]),
            "market state when tracking ended",
        )
    for name in ("corporate_actions_in_window", "events_in_window"):
        entries = context.get(name) or []
        if entries:
            facts.put(
                f"case.{name}",
                f"context.{name}",
                entries,
                count(len(entries)),
                name.replace("_", " "),
            )
    return facts


def merge(*sets: FactSet) -> FactSet:
    """One registry from several extractors. Later keys never overwrite —
    a duplicate key would mean two inputs claim the same fact, and
    silently preferring one would hide the disagreement."""
    merged = FactSet()
    for source in sets:
        for key, fact in source.facts.items():
            if key in merged:
                continue
            merged.add(fact)
    return merged


def _render_detail(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(entry).replace("_", " ") for entry in value)
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)
