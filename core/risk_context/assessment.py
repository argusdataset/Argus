"""The module's output: itemized risk inputs, and no number that combines them.

## Why there is no risk score here

Module 13 owns `risk_score`. That is not an arbitrary division of labour.
The moment this module emits a single number, three things follow: every
consumer reads the number instead of the items, the weighting that
produced it becomes invisible, and the fact that one of the inputs was
*undetermined* rather than *clear* disappears into it. Module 11 made the
same structural refusal for the same reason — `SimilarityEvidence`
exposes no combined statistic — and Module 08 established the underlying
discipline: a missing input has to survive the transformation.

`tests/unit/risk_context/test_no_aggregation.py` asserts this by scanning
the result's own fields, so the refusal is checkable rather than merely
stated.

## What is deliberately not consulted

**Module 11's similarity evidence.** It is genuinely risk-relevant — MFE
and MAE distributions across analogues describe how badly this shape has
gone before — and it is deliberately not read here. Two reasons. It is
already computed properly, carries its own sufficiency state and its own
intervals, and Module 13 can read it directly; passing it through this
module would add a layer where a `None` median could be misread as a zero
and where the cross-asset/same-asset separation could quietly collapse.
And until Module 17's scan populates the case dataset there is nothing
there to read, so a pass-through would ship an input that is empty by
construction. The honest version of "no similarity evidence yet" is not
to plumb an empty channel through here.

**Module 10's `confidence`.** State facts only — see `invalidation.py`.

**Module 09's bankruptcy verdict.** A hard gate, already applied upstream.
Every candidate reaching this module passed it; re-deriving it would be
two implementations of one judgement.

## Availability is part of the answer

`missing_inputs()` names every input that could not be read and why, in
the shape Module 08's `FeatureEvidence` established. A caller that ignores
it and reads the flags anyway will find `raised=None`, not `False` —
there is no arrangement of this object that lets absent data look like
measured safety.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Connection

from core.data_validation.result import MissReason
from core.feature_engine.vector import FeatureVector
from core.risk_context.config import RiskConfig
from core.risk_context.events import PendingEventsView, pending_events_as_of
from core.risk_context.flags import (
    EVENT_PROXIMITY,
    FLAG_NAMES,
    LIQUIDITY_DEGREE,
    VOLATILITY_SPIKE,
    RiskFlag,
    event_proximity_flag,
    liquidity_degree_flag,
    volatility_spike_flag,
)
from core.risk_context.invalidation import InvalidationSignals, assess_invalidation

#: Written into every result, matching Modules 10 and 11. Every threshold
#: behind these flags is an invented magnitude that has never met outcome
#: data.
CALIBRATION_STATUS = "UNVALIDATED_PLACEHOLDERS"


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Every risk input ARGUS has for one candidate at one instant.

    Named fields only. There is no aggregate, no total, no severity and no
    score — see the module docstring, and the test that enforces it.
    """

    security_id: UUID
    as_of: datetime
    flags: dict[str, RiskFlag]
    events: PendingEventsView
    invalidation: InvalidationSignals
    #: The configuration these verdicts were produced under, so a stored
    #: result can be re-derived. Same discipline as Modules 08-11.
    config_version: str
    calibration_status: str = CALIBRATION_STATUS
    metadata: dict[str, Any] = field(default_factory=dict)

    def flag(self, name: str) -> RiskFlag:
        return self.flags[name]

    def raised_flags(self) -> tuple[str, ...]:
        """Flags that fired. Undetermined ones are absent from this, and
        are not the same as flags that did not fire."""
        return tuple(name for name in FLAG_NAMES if self.flags[name].raised is True)

    def undetermined_flags(self) -> tuple[str, ...]:
        return tuple(name for name in FLAG_NAMES if self.flags[name].raised is None)

    def missing_inputs(self) -> dict[str, MissReason]:
        """Every input that could not be read, and why.

        The shape Module 08's `FeatureEvidence.missing_inputs` established,
        so a caller that already handles one handles this.
        """
        missing = {
            name: self.flags[name].unavailable
            for name in FLAG_NAMES
            if self.flags[name].unavailable is not None
        }
        if self.invalidation.eligibility.runs_observed == 0:
            missing["eligibility_history"] = MissReason.NEVER_INGESTED
        if self.invalidation.transitions_observed == 0:
            missing["state_history"] = MissReason.NEVER_INGESTED
        return {name: reason for name, reason in missing.items() if reason is not None}

    @property
    def complete(self) -> bool:
        """Whether every input was readable. Not a quality judgement."""
        return not self.missing_inputs()

    def as_dict(self) -> dict[str, Any]:
        """The whole assessment, flat enough to store as JSONB."""
        return {
            "security_id": str(self.security_id),
            "as_of": self.as_of.isoformat(),
            "config_version": self.config_version,
            "calibration_status": self.calibration_status,
            "flags": {name: self.flags[name].as_dict() for name in FLAG_NAMES},
            "raised_flags": list(self.raised_flags()),
            "undetermined_flags": list(self.undetermined_flags()),
            "missing_inputs": {
                name: reason.value for name, reason in self.missing_inputs().items()
            },
            "events": self.events.as_dict(),
            "invalidation": self.invalidation.as_dict(),
            "metadata": dict(self.metadata),
            "note": (
                "Itemized inputs only. Module 13 combines these into "
                "risk_score; this module deliberately emits no aggregate."
            ),
        }


def assess_risk_context(
    connection: Connection,
    security_id: UUID,
    *,
    as_of: datetime,
    features: FeatureVector | None = None,
    config: RiskConfig | None = None,
) -> RiskContext:
    """Every risk input for one candidate, as knowable at `as_of`.

    `as_of` is a plain argument — the same call answers for today or for a
    replay date, per `CROSS_CUTTING_REQUIREMENTS.md`.

    `features` is Module 08's vector for this security at this date.
    Omitting it is legitimate and does not fail: the two feature-driven
    flags come back undetermined with a reason, which is the honest answer
    and is exactly what `missing_inputs()` will then report.

    Per candidate rather than per universe, deliberately. Module 09 has
    already reduced ten thousand securities to a candidate list by the
    time anything calls this, and the four queries per candidate here buy
    a much simpler module than batching would. If a caller ever needs this
    across a whole universe, that is the point to revisit it — not before.
    """
    config = config or RiskConfig()
    thresholds = config.thresholds

    events = pending_events_as_of(connection, security_id, as_of=as_of, thresholds=thresholds)
    values = features.features if features is not None else None

    flags = {
        LIQUIDITY_DEGREE: liquidity_degree_flag(values, thresholds),
        VOLATILITY_SPIKE: volatility_spike_flag(values, thresholds),
        EVENT_PROXIMITY: event_proximity_flag(events, thresholds),
    }

    return RiskContext(
        security_id=security_id,
        as_of=as_of,
        flags=flags,
        events=events,
        invalidation=assess_invalidation(connection, security_id, as_of=as_of),
        config_version=config.version_label(),
        metadata={
            "feature_vector_present": features is not None,
            "feature_evidence": _feature_evidence(features),
            "similarity_evidence_consulted": False,
            "similarity_note": (
                "Module 11's evidence is deliberately not read here — it "
                "carries its own sufficiency state and intervals, and "
                "Module 13 reads it directly. See the module docstring."
            ),
        },
    )


def _feature_evidence(features: FeatureVector | None) -> dict[str, Any] | None:
    """Module 08's own account of what it could and could not compute.

    Carried through verbatim rather than re-summarised: a flag reading
    `raised=None` because `atr_percentile` was uncomputable is best
    explained by the evidence record that says why it was uncomputable.
    """
    if features is None:
        return None
    evidence = features.evidence
    return {
        "bars_available": evidence.bars_available,
        "bars_required": evidence.bars_required,
        "coverage_ratio": evidence.coverage_ratio,
        "missing_inputs": {name: reason.value for name, reason in evidence.missing_inputs.items()},
        "unavailable_features": list(evidence.unavailable_features),
    }
