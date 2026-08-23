"""The eight components, each computed from its own named upstream source.

Every component returns 0..100 or **None**. None means "this could not be
measured", and it is never a zero — the discipline Module 08 established
for features, Module 09 for eligibility, Module 11 for statistics and
Module 12 for risk flags. Here it has one more consequence: a None
component removes its weight from the composite, and if enough weight
disappears the candidate is not scored at all (see `gating.py`).

## Every component keeps its raw readings

`Component.inputs` holds what came in and `Component.normalized` holds
what each reading became. Module 03's comment on the `signals` table says
a user can always see why a score is what it is; the seven stored columns
give the component values, and these two dictionaries are how the layer
below them stays inspectable.

## Two things this module refuses to read

**Module 10's state `confidence`.** It is that module's target-model
`quality`, and it enters here only as the Pattern Quality *component* —
never as evidence about reliability. Module 10's own report warned against
treating it as a measured quantity, and ARGUS `confidence` is computed in
`confidence.py` from sample sizes, interval widths and coverage instead.

**Same-asset similarity.** Module 11 keeps cross-asset and same-asset
evidence structurally separate and exposes no combined statistic. The
Historical Evidence component reads **cross-asset only**: the question it
asks is "how have comparable setups resolved", which is a cross-sectional
claim. A security's own three prior attempts are its own history, already
surfaced by Module 12's invalidation signals, and summing them into a
cross-sectional statistic is the exact collapse Module 11 built its type
structure to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.feature_engine.vector import FeatureVector
from core.historical_similarity.engine import SimilarityEvidence
from core.historical_similarity.statistics import SampleSufficiency
from core.market_state.target_model_matching.interface import TargetModelAssessment
from core.risk_context.assessment import RiskContext
from core.risk_context.flags import EVENT_PROXIMITY, LIQUIDITY_DEGREE, VOLATILITY_SPIKE
from core.scoring.config import SCORE_MAX, Normalizers, ScoringConfig

#: Component names. They match the `signals` column suffixes so the
#: mapping from a computed component to its stored column is by name
#: rather than by position.
PATTERN_QUALITY = "pattern_quality"
HISTORICAL_EVIDENCE = "historical_evidence"
MARKET_REGIME = "market_regime"
VOLUME_LIQUIDITY = "volume_liquidity"
VOLATILITY_STRUCTURE = "volatility_structure"
FUNDAMENTAL_CONTEXT = "fundamental_context"
RISK_REWARD = "risk_reward"

COMPONENT_NAMES: tuple[str, ...] = (
    PATTERN_QUALITY,
    HISTORICAL_EVIDENCE,
    MARKET_REGIME,
    VOLUME_LIQUIDITY,
    VOLATILITY_STRUCTURE,
    FUNDAMENTAL_CONTEXT,
    RISK_REWARD,
)

#: Module 08 computes these two from identical arithmetic under two names
#: in two feature groups: `volatility_contraction_onset` (Group A) and
#: `volatility_compression` (Group B) are both
#: `safe_ratio(recent_volatility, recent_volatility.shift(medium))`.
#: Module 11 flagged the duplication and this module inherits it.
#:
#: **Not fixed at source** — that is Module 08's call, and changing a
#: feature name would invalidate every stored `feature_schema_version`.
#: **Not silently double-counted either**: the key is the name this module
#: refuses to read, the value is the one it reads instead, and
#: `tests/unit/scoring/test_duplicate_features.py` asserts both that the
#: two really are identical and that no component's input list contains
#: a suppressed name.
DUPLICATE_FEATURES: dict[str, str] = {
    "volatility_contraction_onset": "volatility_compression",
}

#: Features each component reads, declared so the duplicate check can be
#: made against data rather than against a reading of the code.
#: Features target-model-v1 reads inside its own `quality`, mirrored here
#: so this module can check itself against them. Two of its four internal
#: sub-components — `prior_decline` (`peak_to_trough_decline`) and
#: `stabilization` (`downside_momentum_reduction`,
#: `volatility_contraction_onset`) — already cover the prior decline.
#:
#: This is why the Module 13 brief's open 10% was resolved by
#: redistribution rather than by an eighth "prior decline" component: that
#: component's obvious inputs are already inside Pattern Quality, and
#: adding them again would give the decline roughly a third of the score
#: while appearing to measure something new.
TARGET_MODEL_INTERNAL_FEATURES: tuple[str, ...] = (
    "peak_to_trough_decline",
    "downside_momentum_reduction",
    "volatility_contraction_onset",
    "volatility_compression",
    "normalized_range_width",
    "volume_expansion",
    "resistance_pressure",
)

COMPONENT_FEATURES: dict[str, tuple[str, ...]] = {
    MARKET_REGIME: (
        "market_regime_trend",
        "market_regime_volatility",
        "market_regime_drawdown",
    ),
    VOLUME_LIQUIDITY: ("avg_dollar_volume", "spread_proxy"),
    VOLATILITY_STRUCTURE: (
        "volatility_compression",
        "atr_percentile",
        "normalized_range_width",
    ),
}


@dataclass(frozen=True, slots=True)
class Component:
    """One component's value, its weight, and everything behind it."""

    name: str
    #: 0..100, or None when it could not be measured.
    value: float | None
    weight: float
    #: Raw upstream readings, by name.
    inputs: dict[str, float | None] = field(default_factory=dict)
    #: What each reading became after its ramp.
    normalized: dict[str, float | None] = field(default_factory=dict)
    #: Named readings that were absent.
    unavailable: tuple[str, ...] = ()
    #: Why the component is None, or any qualification on its value.
    note: str = ""

    @property
    def measured(self) -> bool:
        return self.value is not None

    @property
    def contributes(self) -> bool:
        """Whether this component participates in the composite at all.

        A zero-weight component neither contributes nor counts as missing
        weight — `fundamental_context` is inactive by design and must not
        drag a candidate toward INSUFFICIENT_EVIDENCE.
        """
        return self.weight > 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "weight": self.weight,
            "inputs": dict(self.inputs),
            "normalized": dict(self.normalized),
            "unavailable": list(self.unavailable),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ScoringInputs:
    """Everything the components read, gathered by the caller.

    Passed in rather than fetched here: the engine owns I/O, and a
    component that could reach the database would be a component nobody
    could test in isolation. Every field is optional because every one of
    them is legitimately absent for some candidate, and saying so is this
    module's job.
    """

    risk: RiskContext
    features: FeatureVector | None = None
    assessment: TargetModelAssessment | None = None
    similarity: SimilarityEvidence | None = None


def compute_components(inputs: ScoringInputs, config: ScoringConfig) -> dict[str, Component]:
    """All eight components for one candidate."""
    weights = config.weights.as_dict()
    ramps = config.normalizers
    return {
        PATTERN_QUALITY: pattern_quality(inputs, ramps, weights[PATTERN_QUALITY]),
        HISTORICAL_EVIDENCE: historical_evidence(inputs, ramps, weights[HISTORICAL_EVIDENCE]),
        MARKET_REGIME: market_regime(inputs, ramps, weights[MARKET_REGIME]),
        VOLUME_LIQUIDITY: volume_liquidity(inputs, ramps, weights[VOLUME_LIQUIDITY]),
        VOLATILITY_STRUCTURE: volatility_structure(inputs, ramps, weights[VOLATILITY_STRUCTURE]),
        FUNDAMENTAL_CONTEXT: fundamental_context(weights[FUNDAMENTAL_CONTEXT]),
        RISK_REWARD: risk_reward(inputs, ramps, weights[RISK_REWARD]),
    }


# --------------------------------------------------------------------------
# Individual components
# --------------------------------------------------------------------------


def pattern_quality(inputs: ScoringInputs, ramps: Normalizers, weight: float) -> Component:
    """Module 10's target-model-v1 match quality, scaled to 0..100.

    The model's `quality` is an unvalidated pattern-match number, which is
    exactly what a Pattern Quality component should be — a judgement about
    shape. What it must not become is a claim about reliability, and it
    does not: nothing in `confidence.py` reads it.
    """
    assessment = inputs.assessment
    raw = assessment.quality if assessment is not None else None
    value = ramps.pattern_quality.apply(raw)
    return Component(
        name=PATTERN_QUALITY,
        value=value,
        weight=weight,
        inputs={"target_model_quality": raw},
        normalized={"target_model_quality": value},
        unavailable=() if value is not None else ("target_model_quality",),
        note=(
            ""
            if value is not None
            else "No target-model assessment: the security's state is outside the "
            "model's covered slice, or too few of its inputs were present."
        ),
    )


def historical_evidence(inputs: ScoringInputs, ramps: Normalizers, weight: float) -> Component:
    """Module 11's cross-asset outcome statistics, interval-aware.

    Two properties make this honest rather than merely computed:

    **Sufficiency gates it.** `INSUFFICIENT` means Module 11 reports no
    statistics at all, so there is nothing to normalize and the component
    is None. That absence is not a low score — it removes 20% of the
    weight, which under the coverage threshold is enough on its own to
    route the candidate to INSUFFICIENT_EVIDENCE.

    **The interval travels with the number.** The ramp is applied to the
    *upper* bound of the Wilson interval, not the point estimate. A 0.6
    failure rate from 6 cases and from 200 cases are the same point
    estimate and score very differently, because the first one's
    pessimistic bound is far worse. This is what "the interval is part of
    the input, not metadata to discard" means in code.
    """
    evidence = inputs.similarity
    if evidence is None:
        return Component(
            name=HISTORICAL_EVIDENCE,
            value=None,
            weight=weight,
            unavailable=("similarity_evidence",),
            note="No similarity search was run for this candidate.",
        )

    statistics = evidence.cross_asset.statistics
    sufficiency = statistics.sufficiency
    if sufficiency is SampleSufficiency.INSUFFICIENT:
        return Component(
            name=HISTORICAL_EVIDENCE,
            value=None,
            weight=weight,
            inputs={"cross_asset_count": float(statistics.sample_count)},
            unavailable=("failure_rate",),
            note=(
                f"Cross-asset sample is {sufficiency.value}: Module 11 reports no "
                "statistics below its floor, so there is nothing to score. Expected "
                "for nearly every candidate until Module 17 populates the case "
                "dataset."
            ),
        )

    interval = statistics.failure_rate_interval
    if interval is None:
        return Component(
            name=HISTORICAL_EVIDENCE,
            value=None,
            weight=weight,
            inputs={"cross_asset_count": float(statistics.sample_count)},
            unavailable=("failure_rate_interval",),
            note=(
                "Analogues were found but none had resolved into a success or "
                "failure, so no failure rate exists to score."
            ),
        )

    pessimistic = interval.high
    value = ramps.historical_failure.apply(pessimistic)
    return Component(
        name=HISTORICAL_EVIDENCE,
        value=value,
        weight=weight,
        inputs={
            "failure_rate": statistics.failure_rate,
            "failure_rate_upper_bound": pessimistic,
            "failure_rate_interval_width": interval.width,
            "cross_asset_count": float(statistics.sample_count),
            "resolved_count": float(statistics.resolved_count),
        },
        normalized={"failure_rate_upper_bound": value},
        note=(
            f"Scored from the upper bound of the Wilson interval "
            f"({pessimistic:.3f}), not the point estimate "
            f"({statistics.failure_rate:.3f}). Sufficiency: {sufficiency.value}. "
            "Cross-asset only — same-asset evidence is deliberately not summed in."
        ),
    )


def market_regime(inputs: ScoringInputs, ramps: Normalizers, weight: float) -> Component:
    """Module 08 Group E, as three equally-weighted named readings.

    Equal weighting is itself an invented choice, stated here rather than
    hidden: there is no evidence that a market drawdown matters as much as
    market volatility, and no way to find out until Module 17.
    """
    return _from_features(
        MARKET_REGIME,
        inputs,
        weight,
        {
            "market_regime_trend": ramps.regime_trend,
            "market_regime_volatility": ramps.regime_volatility,
            "market_regime_drawdown": ramps.regime_drawdown,
        },
        note="Three readings averaged equally — an invented weighting.",
    )


def volume_liquidity(inputs: ScoringInputs, ramps: Normalizers, weight: float) -> Component:
    """Module 08's dollar volume and spread proxy.

    Distinct from the risk component's liquidity reading, and deliberately
    computed from Module 08's raw feature rather than from Module 12's
    normalized degree: this asks whether the setup is worth taking, the
    risk side asks what it costs to leave, and routing both through one
    normalization would tie two different questions to one ramp.
    """
    return _from_features(
        VOLUME_LIQUIDITY,
        inputs,
        weight,
        {
            "avg_dollar_volume": ramps.dollar_volume,
            "spread_proxy": ramps.spread_proxy,
        },
    )


def volatility_structure(inputs: ScoringInputs, ramps: Normalizers, weight: float) -> Component:
    """Module 08 Group B: how tight and how quiet the base is.

    Reads `volatility_compression` and **not**
    `volatility_contraction_onset`. The two are the same arithmetic under
    two names in two feature groups — see `DUPLICATE_FEATURES`. Averaging
    both would give that one signal two thirds of this component's weight
    while appearing to use three independent readings.
    """
    return _from_features(
        VOLATILITY_STRUCTURE,
        inputs,
        weight,
        {
            "volatility_compression": ramps.volatility_compression,
            "atr_percentile": ramps.atr_percentile,
            "normalized_range_width": ramps.range_width,
        },
        note=(
            "volatility_contraction_onset is deliberately excluded: it duplicates "
            "volatility_compression exactly (see DUPLICATE_FEATURES)."
        ),
    )


def fundamental_context(weight: float) -> Component:
    """Structurally present, inactive by project decision.

    Companies fully in losses produce the moves ARGUS exists to find, so a
    hardcoded fundamental-health penalty would suppress exactly those
    cases. The component stays in the structure at zero weight so Module
    17 can activate it if the evidence justifies it — and, because its
    weight is zero, its absence does not count against weight coverage.
    """
    return Component(
        name=FUNDAMENTAL_CONTEXT,
        value=None,
        weight=weight,
        note=(
            "Inactive by design at 0% weight. Real-market observation "
            "(loss-making companies producing 1000%+ moves) is why; Module 17 "
            "may activate it if statistically justified."
        ),
    )


def risk_reward(inputs: ScoringInputs, ramps: Normalizers, weight: float) -> Component:
    """Module 12's itemized risk inputs, each through its own ramp.

    The complement of `risk_score`, and stated as such rather than
    presented as an independent measurement: both are computed from the
    same normalized risk items, this one facing the other way so that
    higher is better like every other component. There is no reward term
    in it. Adding one would mean dividing an available number by an
    unavailable one for nearly every candidate, producing a ratio driven
    entirely by whichever half happened to exist.
    """
    risk = risk_level(inputs, ramps)
    value = None if risk.value is None else _SCORE_TOP - risk.value
    return Component(
        name=RISK_REWARD,
        value=value,
        weight=weight,
        inputs=dict(risk.inputs),
        normalized=dict(risk.normalized),
        unavailable=risk.unavailable,
        note=(
            "The complement of risk_score, from the same normalized inputs. "
            "Risk side only — reward requires Module 11 outcome data."
        )
        if value is not None
        else risk.note,
    )


# --------------------------------------------------------------------------
# Risk level — shared by `risk_reward` and the standalone `risk_score`
# --------------------------------------------------------------------------


def risk_level(inputs: ScoringInputs, ramps: Normalizers) -> Component:
    """Module 12's three flags on one 0..100 scale, higher meaning riskier.

    Module 12's report was explicit that its degrees are not
    commensurable: a liquidity shortfall on 0..1, a volatility expansion
    as an unbounded multiple, and days until an event are three different
    units. Each gets its own named ramp here, and only after that are they
    averaged.

    An **undetermined** flag makes this None. That is Module 12's binding
    finding: a candidate with unmeasured risk inputs is one ARGUS knows
    nothing about, and it must not be handed a middling risk number that
    lets it out-rank a candidate that was measured and came back clean.

    A flag that is determined and not raised is a different thing
    entirely and contributes its measured value — including the
    `NONE_SCHEDULED` event case, where Module 12 confirmed calendar
    coverage existed and nothing was scheduled.
    """
    risk = inputs.risk
    readings: dict[str, float | None] = {}
    normalized: dict[str, float | None] = {}
    unavailable: list[str] = []

    for name, ramp in (
        (LIQUIDITY_DEGREE, ramps.risk_liquidity),
        (VOLATILITY_SPIKE, ramps.risk_volatility_expansion),
        (EVENT_PROXIMITY, ramps.risk_event_proximity),
    ):
        flag = risk.flags[name]
        if flag.raised is None:
            readings[name] = None
            normalized[name] = None
            unavailable.append(name)
            continue
        if name == EVENT_PROXIMITY and flag.degree is None:
            # Coverage was available and nothing is scheduled — a real
            # negative, so no event risk. Distinguishable from an
            # undetermined flag only because Module 12 kept the two apart.
            readings[name] = None
            normalized[name] = _NO_RISK
            continue
        readings[name] = flag.degree
        normalized[name] = ramp.apply(flag.degree)

    if unavailable:
        return Component(
            name="risk_level",
            value=None,
            weight=_NO_RISK,
            inputs=readings,
            normalized=normalized,
            unavailable=tuple(unavailable),
            note=(
                "Risk inputs undetermined: "
                + ", ".join(unavailable)
                + ". Module 12 reports these as unmeasured rather than safe, and "
                "scoring them as middling would let an unknown candidate outrank a "
                "measured one."
            ),
        )

    return Component(
        name="risk_level",
        value=_mean_of_available(normalized.values()),
        weight=_NO_RISK,
        inputs=readings,
        normalized=normalized,
        note="Three normalized risk readings averaged equally — an invented weighting.",
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

#: The top of the score range, from the configuration module so the
#: complement in `risk_reward` is not an inline constant.
_SCORE_TOP: float = SCORE_MAX
#: The bottom of the same range. Named rather than repeated because
#: "no risk measured here" and "this reading carries no weight" are the
#: same number for entirely different reasons.
_NO_RISK: float = 0.0


def _from_features(
    name: str,
    inputs: ScoringInputs,
    weight: float,
    ramped: dict[str, Any],
    *,
    note: str = "",
) -> Component:
    """A component built from named Module 08 features, each with its ramp."""
    suppressed = [feature for feature in ramped if feature in DUPLICATE_FEATURES]
    if suppressed:  # pragma: no cover - guarded by a unit test
        raise ValueError(
            f"{name} reads {suppressed}, which duplicate other features. See DUPLICATE_FEATURES."
        )

    if inputs.features is None:
        return Component(
            name=name,
            value=None,
            weight=weight,
            unavailable=tuple(ramped),
            note="No feature vector was available for this candidate.",
        )

    readings: dict[str, float | None] = {}
    normalized: dict[str, float | None] = {}
    unavailable: list[str] = []
    for feature, ramp in ramped.items():
        raw = inputs.features.features.get(feature)
        readings[feature] = raw
        scaled = ramp.apply(raw)
        normalized[feature] = scaled
        if scaled is None:
            unavailable.append(feature)

    return Component(
        name=name,
        value=_mean_of_available(normalized.values()),
        weight=weight,
        inputs=readings,
        normalized=normalized,
        unavailable=tuple(unavailable),
        note=note,
    )


def _mean_of_available(values) -> float | None:
    """Mean of the readings that exist, or None if none do.

    Averaging over what is present rather than treating an absent reading
    as zero: a market-regime component with two of three readings is a
    partial measurement, not a bad one. How much of the component was
    measured stays visible in `unavailable`.
    """
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)
