"""Every threshold this module uses, isolated per Module 10's discipline.

**None of these values has been validated.** Same status as Modules 10 and
11, and for the same reason: there is no historical outcome data to
calibrate against until Module 17's scan runs. Every result this module
produces carries `calibration_status: "UNVALIDATED_PLACEHOLDERS"`.

## Kind tagging

- **`structural`** — follows from how something is defined rather than
  from a judgement about markets. Changing it changes what the number
  *means*.
- **`calibratable`** — an invented magnitude. Recalibration starts here.

## Why this module has so few thresholds

It is deliberately thin. Most of ARGUS's risk information already exists
elsewhere in more rigorous form — Module 09's bankruptcy gate excludes the
worst cases upstream, and Module 11's MFE/MAE distributions are themselves
a risk signal. This module adds only the signals that belong nowhere else,
and every threshold it introduces is one more unvalidated number that
Module 13 will have to reason about. Fewer is better.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import timedelta
from typing import Any

#: Follows from a definition; changing it changes meaning.
STRUCTURAL = "structural"
#: An invented magnitude. Recalibration starts here.
CALIBRATABLE = "calibratable"


@dataclass(frozen=True, slots=True)
class RiskThreshold:
    """One named threshold, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _t(value: float, kind: str, rationale: str) -> RiskThreshold:
    return RiskThreshold(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class RiskThresholds:
    """The complete threshold set. Enumerable as a whole."""

    # -- Pending material events -----------------------------------------
    imminent_event_days: RiskThreshold = field(
        default_factory=lambda: _t(
            14.0,
            CALIBRATABLE,
            "Days until a scheduled event below which it is flagged as "
            "imminent. Two weeks is a guess at 'close enough that the "
            "event, not the structure, will decide the next move'. Nothing "
            "measured it.",
        )
    )
    event_horizon_days: RiskThreshold = field(
        default_factory=lambda: _t(
            90.0,
            CALIBRATABLE,
            "How far ahead to look for scheduled events at all. Beyond a "
            "quarter, an earnings date is a near-certainty rather than "
            "information — every company has one.",
        )
    )

    #: Lag between ARGUS observing an earnings calendar and being able to
    #: act on it. Applied when ingesting, via `PitTimestamps.derive`.
    ingestion_lag: RiskThreshold = field(
        default_factory=lambda: _t(
            12.0,
            CALIBRATABLE,
            "Hours between fetching an earnings calendar and treating it as "
            "actionable. Conservative in the same direction as Module 05's "
            "lag policy: erring late costs a missed signal, erring early "
            "fabricates foreknowledge.",
        )
    )

    # -- Liquidity degree -------------------------------------------------
    comfortable_dollar_volume: RiskThreshold = field(
        default_factory=lambda: _t(
            1_000_000.0,
            CALIBRATABLE,
            "Average daily dollar volume at which liquidity stops being a "
            "risk consideration. Distinct from Module 09's $50,000 "
            "execution floor, which is pass/fail: this is the top of the "
            "range over which degree still matters. A name at $60,000 "
            "passed the gate and is still far riskier to exit than one at "
            "$5,000,000.",
        )
    )

    # -- Volatility spike --------------------------------------------------
    volatility_spike_percentile: RiskThreshold = field(
        default_factory=lambda: _t(
            0.90,
            CALIBRATABLE,
            "ATR against the security's OWN trailing distribution. Above "
            "this, current volatility is extreme for this name — which may "
            "mean something changed since its features described a quiet "
            "base.",
        )
    )
    volatility_spike_expansion: RiskThreshold = field(
        default_factory=lambda: _t(
            1.50,
            CALIBRATABLE,
            "Recent volatility as a multiple of the base period's. Required "
            "*alongside* the percentile: a security whose ATR percentile is "
            "high but stable has always been volatile, which is a "
            "characteristic rather than a spike.",
        )
    )

    # -- Evidence floor ----------------------------------------------------
    min_inputs_for_assessment: RiskThreshold = field(
        default_factory=lambda: _t(
            0.0,
            STRUCTURAL,
            "Zero on purpose: this module never refuses to produce a "
            "result. It reports what it could and could not measure, per "
            "input, and lets Module 13 decide. A refusal here would hide "
            "the partial information that is the module's whole output.",
        )
    )

    # ---------------------------------------------------------------------
    # Whole-set access. A recalibration tool uses these and nothing else.
    # ---------------------------------------------------------------------

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

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> RiskThresholds:
        """Rebuild the exact values a stored result was produced under."""
        stored = definition.get("thresholds", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = RiskThreshold(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)

    @property
    def ingestion_lag_delta(self) -> timedelta:
        """`ingestion_lag` as a timedelta, for `PitTimestamps.derive`."""
        return timedelta(hours=self.ingestion_lag.value)


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """The module's complete, versioned configuration."""

    name: str = "argus-risk-context"
    thresholds: RiskThresholds = field(default_factory=RiskThresholds)

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "thresholds": self.thresholds.as_dict(),
            "calibration_status": "UNVALIDATED_PLACEHOLDERS",
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"
