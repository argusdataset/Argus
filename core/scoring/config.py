"""Every weight, ramp and threshold this module uses, in one object.

**None of these numbers has been validated.** They are placeholders in
exactly the sense Module 10's state thresholds were, and for the same
reason: there is no historical outcome data to calibrate against until
Module 17's scan runs. Every stored signal cites the
`scoring_configuration` row that produced it, and that row's definition
carries `calibration_status: "UNVALIDATED_PLACEHOLDERS"`.

## Three kinds of number live here, and they are different

**Component weights** — how much each of the eight components counts
toward `argus_score`. Module 10's report described `magnitudes()` as "the
shape a weight-tuning tool should expect"; `ScoringWeights` is that shape.

**Normalization ramps** — how a raw upstream reading becomes a 0..100
component. Module 12's report was explicit that its risk degrees are not
commensurable (liquidity shortfall 0-1, volatility expansion an unbounded
multiple, event proximity in days), that any combination requires
normalization first, and that **the normalization is itself an
unvalidated choice**. So each ramp is named, kind-tagged and versioned
alongside the weights rather than being a formula inside a component.

**Gating thresholds** — how much of the weight must be measurable before
scoring is honest at all.

## Kind tagging

- **`structural`** — follows from a definition. A pattern-match quality is
  0..1 because the model defines it that way; a failure rate maps to a
  quality score in the descending direction because that is what a
  failure rate means. Changing these changes meaning.
- **`calibratable`** — an invented magnitude. Recalibration starts here.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from infra.db.schema.versioning import scoring_configuration

#: Follows from a definition; changing it changes meaning.
STRUCTURAL = "structural"
#: An invented magnitude. Recalibration starts here.
CALIBRATABLE = "calibratable"

#: The 0..100 range every score and component occupies. Definitional —
#: Module 03's CHECK constraints enforce it on every score column.
SCORE_MAX = 100.0

#: The outcome `probability` would refer to, if a calibrated model
#: existed. Stored with every signal so the structure is present and the
#: absence of a number is unambiguous.
PROBABILITY_DEFINITION = "+10% before -5% within 60 trading days"

#: Appended to the stored definition because `signals` has no JSONB
#: column to carry it. See the module README on that gap.
PROBABILITY_STATUS = "NOT_YET_CALIBRATED: no model exists (Module 17 prerequisite)"

#: How the two are joined in `signals.probability_definition`.
PROBABILITY_DEFINITION_SEPARATOR = " | "


def stored_probability_definition() -> str:
    """What goes in `signals.probability_definition`, with its status."""
    return f"{PROBABILITY_DEFINITION}{PROBABILITY_DEFINITION_SEPARATOR}{PROBABILITY_STATUS}"


# --------------------------------------------------------------------------
# Weights
# --------------------------------------------------------------------------


#: The seven component shares exactly as the Module 13 brief states them.
#: They sum to 0.90 — the brief left 10% deliberately unassigned and asked
#: for the gap to be closed by a named choice rather than silently.
#:
#: **The choice made here is proportional redistribution**, and it is
#: executed by `_share()` below rather than written out as seven
#: hand-computed constants, so the rule stays auditable and the original
#: figures stay visible. Proportional scaling is the only redistribution
#: that leaves every pairwise ratio between components unchanged: it adds
#: no information the brief did not already contain, which is the most
#: that can honestly be claimed for a set of numbers nothing has
#: validated. Rounding the results to tidy percentages would be a second
#: invented decision on top of it, so they are left exact.
#:
#: The alternative the brief also permits — a named eighth component — was
#: considered and rejected on two specific grounds, both recorded in the
#: module README: target-model-v1 already scores `prior_decline` and
#: `stabilization` internally, so the obvious candidate would double-count
#: Pattern Quality; and Module 03's `signals` table has exactly seven
#: component columns, so an eighth could not be stored and the stored
#: breakdown would no longer reconstruct the score.
BRIEF_SHARES: dict[str, float] = {
    "pattern_quality": 0.25,
    "historical_evidence": 0.20,
    "market_regime": 0.15,
    "volume_liquidity": 0.10,
    "volatility_structure": 0.10,
    "fundamental_context": 0.0,
    "risk_reward": 0.10,
}

#: What the brief left unassigned. Named so the gap is greppable.
UNASSIGNED_SHARE = 1.0 - sum(BRIEF_SHARES.values())


def _share(name: str) -> float:
    """The brief's share for `name`, scaled so the active set sums to 1."""
    return BRIEF_SHARES[name] / sum(BRIEF_SHARES.values())


@dataclass(frozen=True, slots=True)
class ComponentWeight:
    """One component's share of `argus_score`, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _w(value: float, kind: str, rationale: str) -> ComponentWeight:
    return ComponentWeight(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """The seven component weights. Enumerable as a whole set.

    The values are the brief's shares scaled by `_share()` so the active
    set sums to 1 — see `BRIEF_SHARES` for the reasoning, and note that
    the scaling changes no pairwise ratio between components.
    """

    pattern_quality: ComponentWeight = field(
        default_factory=lambda: _w(
            _share("pattern_quality"),
            CALIBRATABLE,
            "Module 10's target-model-v1 match quality. The largest single "
            "share because the pattern is what ARGUS claims to recognise — "
            "and entirely unvalidated, because the model's own weights are. "
            "Brief share 0.25, scaled.",
        )
    )
    historical_evidence: ComponentWeight = field(
        default_factory=lambda: _w(
            _share("historical_evidence"),
            CALIBRATABLE,
            "Module 11's cross-asset outcome statistics. Second largest, "
            "and today almost always unavailable: until Module 17 runs "
            "there are no concluded cases to compare against. Brief share "
            "0.20, scaled.",
        )
    )
    market_regime: ComponentWeight = field(
        default_factory=lambda: _w(
            _share("market_regime"),
            CALIBRATABLE,
            "Module 08 Group E. A base forming into a market-wide drawdown "
            "is a different proposition from the same base in a healthy "
            "tape; how much different is a guess. Brief share 0.15, scaled.",
        )
    )
    volume_liquidity: ComponentWeight = field(
        default_factory=lambda: _w(
            _share("volume_liquidity"),
            CALIBRATABLE,
            "Module 08's dollar volume and spread proxy. Distinct from the "
            "risk component: this asks whether the setup is worth taking, "
            "the risk side asks what it costs to leave. Brief share 0.10, "
            "scaled.",
        )
    )
    volatility_structure: ComponentWeight = field(
        default_factory=lambda: _w(
            _share("volatility_structure"),
            CALIBRATABLE,
            "Module 08 Group B compression and range. Reads "
            "`volatility_compression` only — see DUPLICATE_FEATURES. Brief "
            "share 0.10, scaled.",
        )
    )
    fundamental_context: ComponentWeight = field(
        default_factory=lambda: _w(
            _share("fundamental_context"),
            STRUCTURAL,
            "Zero by project decision, not by oversight: companies fully "
            "in losses produce the 1000% moves ARGUS exists to find, so a "
            "hardcoded fundamental-health penalty would suppress exactly "
            "those. Structurally present so Module 17 can activate it if "
            "the evidence justifies it — a component at zero is "
            "discoverable, an absent one is not. Scaling zero leaves zero.",
        )
    )
    risk_reward: ComponentWeight = field(
        default_factory=lambda: _w(
            _share("risk_reward"),
            CALIBRATABLE,
            "Module 12's itemized risk inputs, each normalized by its own "
            "named ramp because Module 12's report established they are "
            "not commensurable as raw degrees. Brief share 0.10, scaled.",
        )
    )

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name).value) for f in fields(self)}

    def describe(self) -> dict[str, dict[str, Any]]:
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    def calibratable(self) -> dict[str, float]:
        """Only the invented magnitudes — where recalibration starts."""
        return {
            f.name: float(getattr(self, f.name).value)
            for f in fields(self)
            if getattr(self, f.name).kind == CALIBRATABLE
        }

    def active(self) -> dict[str, float]:
        """Weights above zero. `fundamental_context` is deliberately out."""
        return {name: value for name, value in self.as_dict().items() if value > 0.0}

    def total(self) -> float:
        return sum(self.as_dict().values())

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> ScoringWeights:
        stored = definition.get("weights", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = ComponentWeight(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ramp:
    """A named linear map from a raw upstream reading to 0..100.

    `low` maps to 0 and `high` maps to `SCORE_MAX`; values outside are
    clamped. `low > high` is a legitimate, common configuration — an
    inverted ramp, where a larger reading is worse (a wider spread, more
    volatility, fewer days until a binary event).

    `log_scale` compares orders of magnitude instead of differences. Only
    dollar volume uses it, and it matters there: on a linear ramp from
    $50k to $5M, the entire range between $50k and $500k — where most of
    the universe lives — compresses into the bottom tenth.

    A ramp is not a threshold, and that distinction is why these exist as
    objects rather than as arithmetic inside a component: a threshold
    answers yes or no, a ramp says how far along. Module 12 established
    that the risk degrees feeding this module are not comparable in their
    raw units; these are the named, versioned, individually recalibratable
    choices that make them comparable.
    """

    low: float
    high: float
    kind: str
    rationale: str
    log_scale: bool = False

    def apply(self, value: float | None) -> float | None:
        """The reading as 0..100, or None if there was no reading.

        None in, None out — never a zero standing in for an absent
        measurement, per the discipline Modules 08 and 12 established.
        """
        if value is None:
            return None
        reading, low, high = float(value), self.low, self.high
        if self.log_scale:
            if reading <= 0.0 or low <= 0.0 or high <= 0.0:
                return None
            reading, low, high = math.log10(reading), math.log10(low), math.log10(high)
        if high == low:
            return SCORE_MAX
        fraction = (reading - low) / (high - low)
        return SCORE_MAX * min(1.0, max(0.0, fraction))

    def as_dict(self) -> dict[str, Any]:
        return {"low": self.low, "high": self.high, "log_scale": self.log_scale}


def _r(low: float, high: float, kind: str, rationale: str, *, log: bool = False) -> Ramp:
    return Ramp(low=low, high=high, kind=kind, rationale=rationale, log_scale=log)


@dataclass(frozen=True, slots=True)
class Normalizers:
    """Every raw-reading-to-0..100 map, named and individually tunable."""

    # -- Pattern quality ---------------------------------------------------
    pattern_quality: Ramp = field(
        default_factory=lambda: _r(
            0.0,
            1.0,
            STRUCTURAL,
            "target-model-v1 defines quality on 0..1, so this is the "
            "identity scaled to the score range. The model's own weights "
            "are unvalidated; this mapping is not where that is fixed.",
        )
    )

    # -- Historical evidence -----------------------------------------------
    historical_failure: Ramp = field(
        default_factory=lambda: _r(
            1.0,
            0.0,
            STRUCTURAL,
            "Applied to the UPPER bound of Module 11's Wilson interval, "
            "not to the point estimate. That is how the interval travels "
            "with the number: a 0.6 failure rate from 6 cases has a "
            "pessimistic bound near 0.9 and scores far worse than the "
            "same 0.6 from 200 cases. Descending because a higher failure "
            "rate is by definition worse; the 0..1 domain is the domain "
            "of a proportion.",
        )
    )

    # -- Market regime ------------------------------------------------------
    regime_trend: Ramp = field(
        default_factory=lambda: _r(
            -0.0010,
            0.0010,
            CALIBRATABLE,
            "Benchmark slope normalized by level, per bar. The bounds are "
            "a guess at 'clearly falling' and 'clearly rising' for a daily "
            "series; nothing measured them.",
        )
    )
    regime_volatility: Ramp = field(
        default_factory=lambda: _r(
            0.45,
            0.10,
            CALIBRATABLE,
            "Annualized benchmark volatility, descending: a base forming "
            "into a violent tape is a worse proposition than the same base "
            "in a calm one. 45% and 10% bracket the range a US equity "
            "index has spent most of its time in — an observation about "
            "history, not a validated boundary.",
        )
    )
    regime_drawdown: Ramp = field(
        default_factory=lambda: _r(
            -0.35,
            0.0,
            CALIBRATABLE,
            "Benchmark distance below its trailing peak, which Module 08 "
            "reports as a negative number. Zero is at the highs.",
        )
    )

    # -- Volume and liquidity ------------------------------------------------
    dollar_volume: Ramp = field(
        default_factory=lambda: _r(
            50_000.0,
            5_000_000.0,
            CALIBRATABLE,
            "Log-scaled. The low end is Module 09's execution floor, so "
            "the ramp starts exactly where the pass/fail gate stops; the "
            "top is where liquidity stops being a consideration. Linear, "
            "the whole $50k-$500k band would collapse into the bottom "
            "tenth of the range.",
            log=True,
        )
    )
    spread_proxy: Ramp = field(
        default_factory=lambda: _r(
            0.12,
            0.01,
            CALIBRATABLE,
            "Module 08's high-low range proxy, descending. It is a proxy "
            "for a spread, not a spread — ARGUS has no quote data — so "
            "these bounds are guesses about a stand-in.",
        )
    )

    # -- Volatility structure -------------------------------------------------
    volatility_compression: Ramp = field(
        default_factory=lambda: _r(
            1.50,
            0.50,
            CALIBRATABLE,
            "Recent volatility over the preceding stretch, descending: "
            "below 1 is coiling, which is the structure this component "
            "rewards.",
        )
    )
    atr_percentile: Ramp = field(
        default_factory=lambda: _r(
            0.90,
            0.10,
            CALIBRATABLE,
            "ATR against the security's own trailing distribution, "
            "descending. Quiet relative to its own history is what a base "
            "looks like.",
        )
    )
    range_width: Ramp = field(
        default_factory=lambda: _r(
            0.60,
            0.05,
            CALIBRATABLE,
            "Range height as a fraction of price, descending. A tight "
            "range is a better-defined base than a loose one.",
        )
    )

    # -- Risk (Module 12's itemized inputs) -----------------------------------
    risk_liquidity: Ramp = field(
        default_factory=lambda: _r(
            0.0,
            1.0,
            STRUCTURAL,
            "Module 12's liquidity degree is already 0 (comfortable) to 1 "
            "(no volume) and already points the risk direction, so this is "
            "the identity scaled to the score range.",
        )
    )
    risk_volatility_expansion: Ramp = field(
        default_factory=lambda: _r(
            1.0,
            3.0,
            CALIBRATABLE,
            "Module 12's expansion multiple. Unbounded above, so the top "
            "of the ramp is a clamp rather than a maximum — an eightfold "
            "expansion and a threefold one both read as maximum risk here, "
            "which is a deliberate simplification.",
        )
    )
    risk_event_proximity: Ramp = field(
        default_factory=lambda: _r(
            90.0,
            0.0,
            CALIBRATABLE,
            "Days until the next scheduled binary event, descending to "
            "zero days. The top matches Module 12's 90-day horizon so the "
            "ramp cannot receive a reading outside its own domain.",
        )
    )

    # -- Opportunity ----------------------------------------------------------
    opportunity_mfe: Ramp = field(
        default_factory=lambda: _r(
            0.0,
            0.50,
            CALIBRATABLE,
            "Mean maximum favourable excursion across analogues. 50% as "
            "the top of the range is an invented anchor and will be one of "
            "the first numbers Module 17 replaces.",
        )
    )

    # -- Confidence factors ----------------------------------------------------
    confidence_sample_size: Ramp = field(
        default_factory=lambda: _r(
            5.0,
            30.0,
            CALIBRATABLE,
            "Cross-asset analogue count. The bounds mirror Module 11's own "
            "refusal floor and comfort level rather than inventing a third "
            "pair, but they are that module's guesses, not measurements.",
        )
    )
    confidence_interval_width: Ramp = field(
        default_factory=lambda: _r(
            0.50,
            0.05,
            CALIBRATABLE,
            "Width of the Wilson interval on the failure rate, descending. "
            "This is the second, direct way the interval enters scoring: "
            "it depresses the component through the pessimistic bound and "
            "depresses confidence through its width.",
        )
    )
    confidence_feature_coverage: Ramp = field(
        default_factory=lambda: _r(
            0.0,
            1.0,
            STRUCTURAL,
            "Module 08's coverage ratio is a fraction by construction.",
        )
    )
    confidence_weight_coverage: Ramp = field(
        default_factory=lambda: _r(
            0.0,
            1.0,
            STRUCTURAL,
            "Share of the active weight that was actually measurable. A fraction by construction.",
        )
    )
    confidence_event_clarity: Ramp = field(
        default_factory=lambda: _r(
            0.0,
            45.0,
            CALIBRATABLE,
            "Days until the next binary event, ascending. Module 12's "
            "report argued event proximity is directional rather than "
            "strictly bad, and belongs in confidence: an imminent earnings "
            "release does not make the structure worse, it makes the "
            "thesis less structurally determined. This is where that "
            "argument is implemented.",
        )
    )

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {f.name: getattr(self, f.name).as_dict() for f in fields(self)}

    def describe(self) -> dict[str, dict[str, Any]]:
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    def calibratable(self) -> tuple[str, ...]:
        return tuple(f.name for f in fields(self) if getattr(self, f.name).kind == CALIBRATABLE)

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> Normalizers:
        stored = definition.get("normalizers", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = Ramp(
                low=float(stored[name]["low"]),
                high=float(stored[name]["high"]),
                kind=existing.kind,
                rationale=existing.rationale,
                log_scale=bool(stored[name].get("log_scale", False)),
            )
        return cls(**overrides)


# --------------------------------------------------------------------------
# Confidence weights — a separate set, deliberately
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfidenceWeights:
    """How the reliability factors combine into `confidence`.

    A separate object from `ScoringWeights` because `confidence` answers a
    different question from `argus_score` and must be able to move
    independently of it. Merging the two sets would make it possible to
    tune one and silently change the other.

    Nothing here reads pattern quality, `argus_score`, or Module 10's
    state confidence. The first two would make confidence a restatement of
    the score; the third is an unvalidated pattern-match number that
    Module 10's own report warned against treating as evidence.
    """

    sample_sufficiency: ComponentWeight = field(
        default_factory=lambda: _w(
            0.30,
            CALIBRATABLE,
            "How many comparable historical cases there were. The largest "
            "share because it is the difference between a claim and an "
            "anecdote.",
        )
    )
    interval_width: ComponentWeight = field(
        default_factory=lambda: _w(
            0.20,
            CALIBRATABLE,
            "How wide the interval around the headline statistic is. "
            "Distinct from sample size: a proportion near 0.5 has a wider "
            "interval than one near 0 at the same n.",
        )
    )
    feature_coverage: ComponentWeight = field(
        default_factory=lambda: _w(
            0.20,
            CALIBRATABLE,
            "How much of the history Module 08's longest window needed was actually present.",
        )
    )
    weight_coverage: ComponentWeight = field(
        default_factory=lambda: _w(
            0.15,
            CALIBRATABLE,
            "How much of the active component weight was measurable. A "
            "score assembled from three quarters of its intended inputs is "
            "less reliable than the same number from all of them.",
        )
    )
    event_clarity: ComponentWeight = field(
        default_factory=lambda: _w(
            0.15,
            CALIBRATABLE,
            "How far away the next scheduled binary event is. Module 12's "
            "finding #4: proximity belongs here rather than only in risk.",
        )
    )

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name).value) for f in fields(self)}

    def describe(self) -> dict[str, dict[str, Any]]:
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    def total(self) -> float:
        return sum(self.as_dict().values())

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> ConfidenceWeights:
        stored = definition.get("confidence_weights", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = ComponentWeight(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)


# --------------------------------------------------------------------------
# Gating thresholds
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScoringThresholds:
    """The numbers that decide whether scoring happens at all."""

    min_weight_coverage: ComponentWeight = field(
        default_factory=lambda: _w(
            0.85,
            CALIBRATABLE,
            "Share of the active component weight that must be measurable "
            "before a composite is honest. Set deliberately above 0.80 so "
            "that losing the historical-evidence component alone (0.20) "
            "routes a candidate to INSUFFICIENT_EVIDENCE rather than to a "
            "score assembled from everything else. Until Module 17 "
            "populates the case dataset that is nearly every candidate, "
            "which is the correct current behaviour and not a bug.",
        )
    )

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name).value) for f in fields(self)}

    def describe(self) -> dict[str, dict[str, Any]]:
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> ScoringThresholds:
        stored = definition.get("thresholds", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = ComponentWeight(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)


# --------------------------------------------------------------------------
# The whole configuration
# --------------------------------------------------------------------------


class WeightsDoNotSumError(ValueError):
    """Component weights must sum to 1. A configuration that does not is
    not a differently-tuned configuration, it is a rescaling of the whole
    score, and two of them would produce incomparable numbers."""


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    """The module's complete, versioned configuration."""

    name: str = "argus-scoring"
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    normalizers: Normalizers = field(default_factory=Normalizers)
    confidence_weights: ConfidenceWeights = field(default_factory=ConfidenceWeights)
    thresholds: ScoringThresholds = field(default_factory=ScoringThresholds)

    def __post_init__(self) -> None:
        for label, total in (
            ("component", self.weights.total()),
            ("confidence", self.confidence_weights.total()),
        ):
            if not math.isclose(total, 1.0, abs_tol=1e-9):
                raise WeightsDoNotSumError(
                    f"{label} weights sum to {total}, not 1.0. Ranking across "
                    "candidates is only meaningful if every score is on the "
                    "same scale."
                )

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weights": self.weights.as_dict(),
            "normalizers": self.normalizers.as_dict(),
            "confidence_weights": self.confidence_weights.as_dict(),
            "thresholds": self.thresholds.as_dict(),
            "probability_definition": PROBABILITY_DEFINITION,
            "probability_status": PROBABILITY_STATUS,
            "calibration_status": "UNVALIDATED_PLACEHOLDERS",
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> ScoringConfig:
        """Rebuild the exact configuration a stored signal cites.

        Without this the append-only `scoring_configuration` table would
        record *that* the weights changed without preserving enough to
        reproduce the old behaviour, which is most of the reason for
        versioning them.
        """
        return cls(
            name=definition.get("name", "argus-scoring"),
            weights=ScoringWeights.from_definition(definition),
            normalizers=Normalizers.from_definition(definition),
            confidence_weights=ConfidenceWeights.from_definition(definition),
            thresholds=ScoringThresholds.from_definition(definition),
        )


def publish_scoring_configuration(
    connection: Connection,
    config: ScoringConfig | None = None,
    *,
    description: str | None = None,
) -> UUID:
    """Get or create the `scoring_configuration` row for this config.

    Idempotent by checksum, exactly like Modules 08, 09, 10 and 11. The
    table is append-only, so a recalibration becomes a new row and never
    edits an existing one — which is what lets a signal written today be
    re-derived years from now from the ID it cites.
    """
    config = config or ScoringConfig()
    checksum = config.content_checksum()

    existing = connection.execute(
        select(scoring_configuration.c.id)
        .where(scoring_configuration.c.content_checksum == checksum)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    return connection.execute(
        scoring_configuration.insert()
        .values(
            version_label=config.version_label(),
            definition=config.definition(),
            content_checksum=checksum,
            description=description or "Unvalidated placeholder weights (Module 13).",
            published_at=datetime.now(UTC),
        )
        .returning(scoring_configuration.c.id)
    ).scalar_one()
