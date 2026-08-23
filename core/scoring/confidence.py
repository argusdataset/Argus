"""`confidence` — how reliable the evidence is, not how good the setup is.

These two numbers are the reason this module exists as more than a
weighted sum. `argus_score` says "this looks like a strong setup";
`confidence` says "and here is how much the evidence behind that is worth".
They must be able to diverge sharply, and the only way to guarantee that
is to compute them from different inputs.

So nothing here reads a component value, `argus_score`, pattern quality,
or Module 10's state confidence. The factors are:

| Factor | Question |
|---|---|
| `sample_sufficiency` | How many comparable historical cases were there? |
| `interval_width` | How wide is the interval around the headline statistic? |
| `feature_coverage` | How much of the history the features needed was present? |
| `weight_coverage` | How much of the score was actually measured? |
| `event_clarity` | How far off is the next scheduled binary event? |

The last one implements Module 12's finding #4 directly. An imminent
earnings release does not make a base worse — it makes the thesis less
structurally determined, because the next move will be decided by a
scheduled binary outcome rather than by the structure ARGUS recognised.
That is a statement about how much to trust the reading, which is what
confidence is for.

A factor that cannot be measured is dropped and its weight redistributed
across the rest, with the omission recorded. Scoring it zero would
conflate "no evidence about this" with "bad on this", which is the exact
error the whole system is built to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.risk_context.events import EventCoverage
from core.scoring.components import ScoringInputs
from core.scoring.config import SCORE_MAX, ScoringConfig

SAMPLE_SUFFICIENCY = "sample_sufficiency"
INTERVAL_WIDTH = "interval_width"
FEATURE_COVERAGE = "feature_coverage"
WEIGHT_COVERAGE = "weight_coverage"
EVENT_CLARITY = "event_clarity"

CONFIDENCE_FACTORS: tuple[str, ...] = (
    SAMPLE_SUFFICIENCY,
    INTERVAL_WIDTH,
    FEATURE_COVERAGE,
    WEIGHT_COVERAGE,
    EVENT_CLARITY,
)


@dataclass(frozen=True, slots=True)
class ConfidenceFactor:
    """One reliability reading, normalized."""

    name: str
    #: 0..100, or None when the factor could not be measured.
    value: float | None
    weight: float
    #: The raw reading behind it, in its own units.
    reading: float | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "weight": self.weight,
            "reading": self.reading,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ConfidenceAssessment:
    """`confidence`, and every factor that produced it."""

    value: float | None
    factors: dict[str, ConfidenceFactor] = field(default_factory=dict)
    #: Share of the confidence weight that was measurable.
    factor_coverage: float = 0.0

    def unavailable(self) -> tuple[str, ...]:
        return tuple(name for name in CONFIDENCE_FACTORS if self.factors[name].value is None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "factor_coverage": self.factor_coverage,
            "unavailable": list(self.unavailable()),
            "factors": {name: self.factors[name].as_dict() for name in CONFIDENCE_FACTORS},
            "note": (
                "Computed from evidence reliability only. Deliberately reads no "
                "component value, no argus_score, and not Module 10's state "
                "confidence."
            ),
        }


def compute_confidence(
    inputs: ScoringInputs,
    *,
    weight_coverage: float,
    config: ScoringConfig,
) -> ConfidenceAssessment:
    """How much the evidence behind this candidate's score is worth."""
    ramps = config.normalizers
    weights = config.confidence_weights.as_dict()

    statistics = inputs.similarity.cross_asset.statistics if inputs.similarity is not None else None
    interval = statistics.failure_rate_interval if statistics is not None else None
    coverage_ratio = (
        inputs.features.evidence.coverage_ratio if inputs.features is not None else None
    )

    sample_count = float(statistics.sample_count) if statistics is not None else None
    factors = {
        SAMPLE_SUFFICIENCY: ConfidenceFactor(
            name=SAMPLE_SUFFICIENCY,
            value=ramps.confidence_sample_size.apply(sample_count),
            weight=weights[SAMPLE_SUFFICIENCY],
            reading=sample_count,
            note=(
                statistics.sufficiency.value if statistics is not None else "no similarity search"
            ),
        ),
        INTERVAL_WIDTH: ConfidenceFactor(
            name=INTERVAL_WIDTH,
            value=ramps.confidence_interval_width.apply(
                interval.width if interval is not None else None
            ),
            weight=weights[INTERVAL_WIDTH],
            reading=interval.width if interval is not None else None,
            note=(
                "Wilson interval on the failure rate"
                if interval is not None
                else "no reportable failure rate"
            ),
        ),
        FEATURE_COVERAGE: ConfidenceFactor(
            name=FEATURE_COVERAGE,
            value=ramps.confidence_feature_coverage.apply(coverage_ratio),
            weight=weights[FEATURE_COVERAGE],
            reading=coverage_ratio,
            note="Module 08 bars available over bars required",
        ),
        WEIGHT_COVERAGE: ConfidenceFactor(
            name=WEIGHT_COVERAGE,
            value=ramps.confidence_weight_coverage.apply(weight_coverage),
            weight=weights[WEIGHT_COVERAGE],
            reading=weight_coverage,
            note="Share of active component weight that was measurable",
        ),
        EVENT_CLARITY: _event_clarity(inputs, config),
    }

    measured = [factor for factor in factors.values() if factor.value is not None]
    total_weight = sum(factor.weight for factor in measured)
    value = (
        None
        if total_weight <= 0.0
        else sum(factor.value * factor.weight for factor in measured) / total_weight
    )

    return ConfidenceAssessment(
        value=value,
        factors=factors,
        factor_coverage=total_weight,
    )


def _event_clarity(inputs: ScoringInputs, config: ScoringConfig) -> ConfidenceFactor:
    """Distance to the next scheduled binary event, as a reliability reading.

    Three outcomes, matching Module 12's `EventCoverage` exactly, because
    the distinction it drew is the one that matters here:

    * an event is known and close — the thesis is less structurally
      determined, so confidence falls;
    * calendar coverage existed and nothing is scheduled — full clarity,
      a real negative;
    * ARGUS could not see the calendar — the factor is unmeasured and
      drops out, rather than being awarded full marks for ignorance.
    """
    events = inputs.risk.events
    weight = config.confidence_weights.as_dict()[EVENT_CLARITY]

    if events.coverage is EventCoverage.UNAVAILABLE:
        return ConfidenceFactor(
            name=EVENT_CLARITY,
            value=None,
            weight=weight,
            note=(
                "No earnings-calendar coverage was knowable at this as_of, so "
                "event clarity is unmeasured rather than clear."
            ),
        )

    if events.coverage is EventCoverage.NONE_SCHEDULED:
        return ConfidenceFactor(
            name=EVENT_CLARITY,
            value=SCORE_MAX,
            weight=weight,
            note="Calendar coverage available; nothing scheduled within the horizon.",
        )

    days = events.days_until_next
    return ConfidenceFactor(
        name=EVENT_CLARITY,
        value=config.normalizers.confidence_event_clarity.apply(days),
        weight=weight,
        reading=days,
        note=(
            "A scheduled binary event does not make the structure worse; it makes "
            "the thesis less structurally determined."
        ),
    )
