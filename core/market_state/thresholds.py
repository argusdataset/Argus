"""Every numeric threshold the state engine uses. All of them. In one place.

## Read this before changing a number here

**None of these values has been statistically validated.** They are
placeholders — reasoned, documented, internally consistent, and entirely
unproven. The calibration work that would justify them (walk-forward
validation, score monotonicity testing, the spike-validation exercise)
has not run. Nothing downstream should present a state assignment as
carrying more confidence than that.

Saying so plainly matters more than it might seem. A threshold that looks
precise — `0.30`, `1.25` — reads as though someone measured something. Not
one of these was measured. They encode a direction the author believed in
("a consolidating security is quieter than it was"), and the magnitude at
which that direction becomes a verdict is a guess.

## Why this file exists at all

Module 10 is the first module in ARGUS whose correctness is not
checkable. "Is BASE_FORMING correctly distinguished from CONSOLIDATION"
is a modeling choice, not a fact — so the engineering discipline moves
from *proving the answer right* to *making the answer cheap to change*.

Hence: no numeric literal appears anywhere in the classification logic.
Every one lives here, named, with the reasoning attached. Recalibration
is then a data change, not an archaeology exercise —
`tests/unit/market_state/test_threshold_isolation.py` enforces this by
scanning the classifier's own source for stray literals, so the rule
cannot rot quietly.

## How a real recalibration would work

1. Read the current values off a published `target_model_version` row —
   `StateThresholds.from_definition()` reconstructs them exactly.
2. Change the numbers. Nothing else.
3. `publish_target_model_version()` writes a new immutable row whose
   `content_checksum` differs, so every state and transition recorded
   under the old values stays attributable to them.
4. Re-run the historical replay. Because `as_of` is a plain argument
   (Module 07's discipline, carried through 08 and 09), the same call
   that classifies today reclassifies 2015.

Nothing about that flow requires touching `classifier.py`.

## Direction vs magnitude

Where a threshold is a *sign* — "volatility is lower than it was", i.e.
compression below 1.0 — the value is a definitional boundary and is
comparatively safe. Where it is a *magnitude* — "compression below 0.75"
— it is a guess. The docstrings below mark which is which, because the
two deserve very different amounts of trust, and a recalibration should
start with the magnitudes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from infra.db.schema.versioning import target_model_version

#: Marks a threshold whose value is a definitional boundary (a sign
#: change, a ratio crossing 1.0) rather than a tuned magnitude. Safer to
#: trust, and not where recalibration should start.
DIRECTIONAL = "directional"
#: Marks a threshold that is an unvalidated magnitude guess. This is most
#: of them. Recalibration starts here.
MAGNITUDE = "magnitude"


@dataclass(frozen=True, slots=True)
class Threshold:
    """One named threshold, its value, and how much to trust it.

    Carrying `kind` and `rationale` alongside the number is the point: a
    bare float in a config file is barely better than a bare float in an
    `if`. What makes recalibration tractable later is knowing which
    numbers were reasoned boundaries and which were invented.
    """

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _t(value: float, kind: str, rationale: str) -> Threshold:
    return Threshold(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class StateThresholds:
    """The complete threshold set for the general state engine.

    Every field is a `Threshold`, so the set is enumerable, inspectable
    and serializable as a whole — `as_dict()` and `magnitudes()` exist
    precisely so a recalibration tool can find every number without
    knowing anything about the state machine.
    """

    # -- DOWN_TREND ------------------------------------------------------
    downtrend_structure: Threshold = field(
        default_factory=lambda: _t(
            0.0,
            DIRECTIONAL,
            "structure_transition below zero means price is falling relative to "
            "its own noise. A sign condition, not a tuned depth.",
        )
    )
    downtrend_lower_low_frequency: Threshold = field(
        default_factory=lambda: _t(
            0.20,
            MAGNITUDE,
            "Fraction of recent bars making new local lows. 0.20 is a guess at "
            "'still actively making lows' versus 'occasionally slipping'.",
        )
    )

    # -- BASE_FORMING ----------------------------------------------------
    base_forming_downside_reduction: Threshold = field(
        default_factory=lambda: _t(
            0.0,
            DIRECTIONAL,
            "downside_momentum_reduction above zero means new lows are getting "
            "rarer than they were. The definition of a decline losing force.",
        )
    )
    base_forming_volatility_contraction: Threshold = field(
        default_factory=lambda: _t(
            1.0,
            DIRECTIONAL,
            "volatility_contraction_onset below 1 means recent volatility is "
            "below the preceding stretch. A ratio crossing unity.",
        )
    )

    # -- CONSOLIDATION ---------------------------------------------------
    consolidation_max_volatility_expansion: Threshold = field(
        default_factory=lambda: _t(
            1.05,
            MAGNITUDE,
            "Recent volatility as a fraction of the preceding stretch, as an "
            "upper bound: volatility must not be materially EXPANDING. "
            "Deliberately near 1.0 rather than below it. A mature base sits "
            "near 1.0 by construction — both windows are inside the quiet "
            "stretch, so nothing is compressing any more. Requiring active "
            "compression here would test the base's *onset*, which "
            "BASE_FORMING already covers, and would leave an established "
            "consolidation matching no state at all. Measured on the "
            "lifecycle fixture: 0.90 in the base versus 2.73 in the "
            "awakening. The quiet *level* is tested by atr_percentile.",
        )
    )
    consolidation_atr_percentile: Threshold = field(
        default_factory=lambda: _t(
            0.40,
            MAGNITUDE,
            "ATR against the security's OWN trailing distribution, so it is "
            "scale-free across securities. 0.40 as 'quiet for this name' is "
            "unvalidated.",
        )
    )
    consolidation_range_width: Threshold = field(
        default_factory=lambda: _t(
            0.25,
            MAGNITUDE,
            "Range height as a fraction of price. 25% is wide enough to admit "
            "volatile microcaps — deliberately loose, since a tight value here "
            "would silently exclude exactly the names ARGUS exists to find.",
        )
    )

    # -- ACCUMULATION ----------------------------------------------------
    accumulation_higher_low_development: Threshold = field(
        default_factory=lambda: _t(
            0.0,
            DIRECTIONAL,
            "Slope of the rolling low, normalized by price. Above zero means "
            "lows are rising — the structural signature of accumulation.",
        )
    )
    accumulation_support_tests: Threshold = field(
        default_factory=lambda: _t(
            2.0,
            MAGNITUDE,
            "How many times the range floor was tested and held. Two is a "
            "guess at 'defended' versus 'happened to stop there once'.",
        )
    )

    # -- BREAKOUT_WATCH --------------------------------------------------
    breakout_watch_volatility_reexpansion: Threshold = field(
        default_factory=lambda: _t(
            1.10,
            MAGNITUDE,
            "Recent volatility rising against the base. Above 1 is expansion by "
            "definition; the 10% margin is invented to avoid firing on noise.",
        )
    )
    breakout_watch_volume_expansion: Threshold = field(
        default_factory=lambda: _t(
            1.15,
            MAGNITUDE,
            "Recent volume against the base. Same shape of guess as above.",
        )
    )
    breakout_watch_resistance_pressure: Threshold = field(
        default_factory=lambda: _t(
            0.60,
            MAGNITUDE,
            "Where price sits in its range, 0 at the low and 1 at the high. "
            "0.60 as 'in the upper portion' is arbitrary.",
        )
    )

    # -- BREAKOUT_READY --------------------------------------------------
    breakout_ready_resistance_pressure: Threshold = field(
        default_factory=lambda: _t(
            0.85,
            MAGNITUDE,
            "Pressing the top of the range hard. Strictly above the "
            "BREAKOUT_WATCH value, which is the part that actually matters — "
            "the ordering is structural, the magnitude is not.",
        )
    )
    breakout_ready_time_in_upper_range: Threshold = field(
        default_factory=lambda: _t(
            0.50,
            MAGNITUDE,
            "Fraction of the recent window spent in the top quarter of the "
            "range. Distinguishes sustained pressure from a single spike.",
        )
    )

    # -- UPTREND ---------------------------------------------------------
    uptrend_breakout_pct: Threshold = field(
        default_factory=lambda: _t(
            0.0,
            DIRECTIONAL,
            "resistance_breakout_pct above zero means price has actually "
            "cleared the level. The definitional boundary of a breakout.",
        )
    )
    uptrend_acceptance: Threshold = field(
        default_factory=lambda: _t(
            0.40,
            MAGNITUDE,
            "Fraction of recent bars closing above the broken level. This is "
            "what separates a held breakout from a one-bar spike, and 0.40 is "
            "a guess at where 'held' begins.",
        )
    )

    # -- DISTRIBUTION ----------------------------------------------------
    distribution_momentum: Threshold = field(
        default_factory=lambda: _t(
            0.0,
            DIRECTIONAL,
            "momentum_improvement below zero: momentum is worse than it was.",
        )
    )
    distribution_drawdown_from_high: Threshold = field(
        default_factory=lambda: _t(
            -0.15,
            MAGNITUDE,
            "How far below its own recent high a security can be and still "
            "count as topping rather than declining. Beyond this it is a "
            "DOWN_TREND, and 15% is an invented dividing line.",
        )
    )

    # -- Evidence floor --------------------------------------------------
    minimum_features_for_classification: Threshold = field(
        default_factory=lambda: _t(
            0.60,
            MAGNITUDE,
            "Fraction of the features a state's predicate reads that must "
            "actually be present. Below this the security is UNCLASSIFIED "
            "rather than classified on fragments — the same refusal Module 09 "
            "makes with INSUFFICIENT_EVIDENCE.",
        )
    )

    # ---------------------------------------------------------------------
    # Whole-set access. A recalibration tool uses these and nothing else.
    # ---------------------------------------------------------------------

    def as_dict(self) -> dict[str, float]:
        """Every threshold name to its bare value."""
        return {f.name: float(getattr(self, f.name).value) for f in fields(self)}

    def describe(self) -> dict[str, dict[str, Any]]:
        """Every threshold with its kind and rationale, for a report or UI."""
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    def magnitudes(self) -> dict[str, float]:
        """Only the unvalidated magnitude guesses — where recalibration starts."""
        return {
            f.name: float(getattr(self, f.name).value)
            for f in fields(self)
            if getattr(self, f.name).kind == MAGNITUDE
        }

    def directional(self) -> dict[str, float]:
        """Only the definitional boundaries — comparatively safe to leave alone."""
        return {
            f.name: float(getattr(self, f.name).value)
            for f in fields(self)
            if getattr(self, f.name).kind == DIRECTIONAL
        }

    @classmethod
    def names(cls) -> tuple[str, ...]:
        """Every threshold name, without instantiating anything."""
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> StateThresholds:
        """Rebuild a threshold set from a stored `target_model_version` row.

        Step 1 of the recalibration walkthrough in this module's docstring:
        a historical state assignment can be re-derived exactly from the
        version it cites, without the current defaults leaking in.
        """
        stored = definition.get("state_thresholds", {})
        overrides: dict[str, Threshold] = {}
        template = cls()
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = Threshold(
                value=float(stored[name]),
                kind=existing.kind,
                rationale=existing.rationale,
            )
        return cls(**overrides)


@dataclass(frozen=True, slots=True)
class MarketStateConfig:
    """The state engine's complete configuration, versioned as a whole.

    Holds the engine's own thresholds plus the target model's, so a single
    published version pins both. They stay *separate objects* because the
    target model is a contained sub-component that a second model would
    one day replace — see `target_model_matching/`.
    """

    name: str = "argus-market-state"
    #: `target-model-v1`'s label. The state engine records this on every
    #: state and transition row, since Module 03 requires it.
    target_model_name: str = "target-model-v1"
    states: StateThresholds = field(default_factory=StateThresholds)
    #: Imported lazily in `definition()` to keep this module free of any
    #: dependency on the target model's internals.
    target_model: Any = None

    def definition(self) -> dict[str, Any]:
        from core.market_state.target_model_matching.models.target_model_v1.thresholds import (
            TargetModelV1Thresholds,
        )

        target = self.target_model or TargetModelV1Thresholds()
        return {
            "name": self.name,
            "target_model_name": self.target_model_name,
            "state_thresholds": self.states.as_dict(),
            "target_model_thresholds": target.as_dict(),
            # Recorded in the version row itself so nobody reading it later
            # mistakes these numbers for measured ones.
            "calibration_status": "UNVALIDATED_PLACEHOLDERS",
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.target_model_name}-{self.content_checksum()[:12]}"


def publish_target_model_version(
    connection: Connection,
    config: MarketStateConfig | None = None,
    *,
    description: str | None = None,
) -> UUID:
    """Get or create the `target_model_version` row for this configuration.

    Idempotent by checksum, matching Module 08's
    `publish_feature_schema_version` and Module 09's
    `publish_detection_configuration`. Append-only, so a recalibration
    becomes a new row and every state recorded under the old thresholds
    stays attributable to them — step 3 of the recalibration walkthrough.
    """
    config = config or MarketStateConfig()
    checksum = config.content_checksum()

    existing = connection.execute(
        select(target_model_version.c.id)
        .where(target_model_version.c.content_checksum == checksum)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    return connection.execute(
        target_model_version.insert()
        .values(
            version_label=config.version_label(),
            definition=config.definition(),
            content_checksum=checksum,
            description=description or "Unvalidated placeholder thresholds (Module 10).",
            published_at=datetime.now(UTC),
        )
        .returning(target_model_version.c.id)
    ).scalar_one()
