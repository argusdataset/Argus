"""Every threshold the lifecycle uses, isolated per Module 10's discipline.

**None of these values has been validated.** Same status as Modules 10,
11, 12 and 13, and for the same reason: there is no historical outcome
data to calibrate against until Module 17's scan runs. Every stored event
payload carries `calibration_status: "UNVALIDATED_PLACEHOLDERS"`.

## What these numbers decide, and what they deliberately do not

They decide **when a tracked setup advances**, not **what gets tracked**.
Detection is driven by Module 10's state assignment, which has its own
thresholds; nothing here can prevent a setup from being created. That
separation is the module's central design decision — see the README on why
requiring a score to open a setup would deadlock the whole system.

## Kind tagging

- **`structural`** — follows from a definition. Changing it changes what
  the number *means*.
- **`calibratable`** — an invented magnitude. Recalibration starts here.
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
class LifecycleThreshold:
    """One named threshold, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _t(value: float, kind: str, rationale: str) -> LifecycleThreshold:
    return LifecycleThreshold(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class QualificationThresholds:
    """The bar a detected setup must clear to become actively tracked."""

    min_argus_score: LifecycleThreshold = field(
        default_factory=lambda: _t(
            60.0,
            CALIBRATABLE,
            "Composite score a detected setup must reach to qualify. An "
            "invented number twice over: the bar itself is a guess, and "
            "the score it is compared against is built from Module 13's "
            "unvalidated weights. Until Module 17 runs, nothing reaches "
            "this bar at all, because nothing gets scored.",
        )
    )
    min_confidence: LifecycleThreshold = field(
        default_factory=lambda: _t(
            50.0,
            CALIBRATABLE,
            "Evidence reliability a setup must reach to qualify, checked "
            "*alongside* the score rather than folded into it. A high "
            "score on thin evidence is structurally possible in Module 13 "
            "by design; this is where ARGUS decides not to act on one.",
        )
    )

    # -- Endpoints ---------------------------------------------------------
    max_active_duration_days: LifecycleThreshold = field(
        default_factory=lambda: _t(
            180.0,
            CALIBRATABLE,
            "How long a setup may stay ACTIVE before the window is treated "
            "as closed. Recognising expiry is this module's job; deciding "
            "that EXPIRED is what happened is Module 15's. Six months is a "
            "guess at 'if the expansion had not started by now, this "
            "attempt is over'.",
        )
    )
    max_detection_duration_days: LifecycleThreshold = field(
        default_factory=lambda: _t(
            365.0,
            CALIBRATABLE,
            "How long a setup may sit detected-but-unqualified. Needed "
            "because detection deliberately does not require a score, so "
            "without it every base ARGUS ever noticed would stay open "
            "forever. A year is long on purpose: a base can take that "
            "long, and closing one early costs exactly the slow setup "
            "ARGUS exists to find.",
        )
    )

    # -- Evidence floor -----------------------------------------------------
    min_events_for_status: LifecycleThreshold = field(
        default_factory=lambda: _t(
            1.0,
            STRUCTURAL,
            "A setup's status is derived from its events, so one event — "
            "the DETECTION that opened it — is the minimum for a status to "
            "exist at all. Not a tuning knob: below this there is no "
            "setup, and above it the derivation would start ignoring real "
            "history.",
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
    def from_definition(cls, definition: dict[str, Any]) -> QualificationThresholds:
        """Rebuild the exact values a stored event was produced under."""
        stored = definition.get("thresholds", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = LifecycleThreshold(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)

    @property
    def active_window(self) -> timedelta:
        return timedelta(days=self.max_active_duration_days.value)

    @property
    def detection_window(self) -> timedelta:
        return timedelta(days=self.max_detection_duration_days.value)


@dataclass(frozen=True, slots=True)
class LifecycleConfig:
    """The module's complete, versioned configuration."""

    name: str = "argus-lifecycle"
    thresholds: QualificationThresholds = field(default_factory=QualificationThresholds)

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
