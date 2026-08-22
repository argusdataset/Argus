"""target-model-v1's own thresholds, separate from the state engine's.

Separate on purpose. The engine's thresholds decide *which state* a
security is in; these decide *how well* it matches this particular
pattern. A second target model would bring its own set and leave the
engine's alone — keeping them in one object would make that impossible
without editing the engine.

Same honesty as `core/market_state/thresholds.py`: **none of these has
been validated.** They are weights and floors chosen to encode a
direction, not measured against outcomes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass(frozen=True, slots=True)
class TargetModelV1Thresholds:
    """Weights and floors for the Long Decline -> Expansion pattern.

    The weights sum to 1.0 and are equal by default. That is deliberate:
    unequal weights would imply someone knows which component matters
    more, and nobody does yet. Equal weighting is the honest prior.
    """

    #: How much of the quality score each phase's evidence contributes.
    weight_prior_decline: float = 0.25
    weight_stabilization: float = 0.25
    weight_consolidation: float = 0.25
    weight_awakening: float = 0.25

    #: Depth of prior decline at which the decline component saturates.
    #: A 60% fall scores the same as an 80% one — beyond some depth the
    #: distinction stops carrying information about the setup.
    decline_saturation: float = 0.60

    #: Volatility compression at which the consolidation component
    #: saturates. Below this, quieter stops meaning better.
    compression_saturation: float = 0.50

    #: Volume expansion at which the awakening component saturates.
    awakening_saturation: float = 2.00

    #: Fraction of the model's inputs that must be present to assess at
    #: all. Below this the model returns `quality=None` rather than a
    #: score built on absences.
    minimum_input_coverage: float = 0.60

    #: Quality at or above which the model considers the structure to
    #: support moving further along the sequence. Recorded as evidence;
    #: the engine does not act on it. See `interface.py` on the
    #: unresolved boundary.
    advancement_quality: float = 0.60

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name)) for f in fields(self)}

    def describe(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> TargetModelV1Thresholds:
        """Rebuild from a stored `target_model_version` row."""
        stored = definition.get("target_model_thresholds", {})
        return cls(**{name: float(stored[name]) for name in cls.names() if name in stored})

    def weights(self) -> dict[str, float]:
        return {
            "prior_decline": self.weight_prior_decline,
            "stabilization": self.weight_stabilization,
            "consolidation": self.weight_consolidation,
            "awakening": self.weight_awakening,
        }
