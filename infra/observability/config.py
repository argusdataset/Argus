"""Every number this module has, isolated per Module 10's discipline.

All of them are `operational` in the strict sense Module 17 defined —
they bound how an observation is *reported*, and provably cannot change
what any ARGUS computation produces. Nothing in this package feeds a
score, a state, a similarity result or a risk assessment; the whole
module is downstream of every decision.

That is worth stating rather than assuming, because "monitoring
thresholds" is exactly the kind of setting that quietly becomes load
-bearing. If a number here ever gates a computation instead of describing
one, it has stopped being operational and the module has stopped
observing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from core.model_validation_evaluation.validation.config import (
    CALIBRATABLE,
    KINDS,
    OPERATIONAL,
    STRUCTURAL,
)

__all__ = [
    "CALIBRATABLE",
    "KINDS",
    "OPERATIONAL",
    "STRUCTURAL",
    "ObservabilityConfig",
    "ObservabilitySetting",
    "ObservabilitySettings",
]


@dataclass(frozen=True, slots=True)
class ObservabilitySetting:
    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value

    def __int__(self) -> int:
        return int(self.value)


def _s(value: float, rationale: str) -> ObservabilitySetting:
    return ObservabilitySetting(value=value, kind=OPERATIONAL, rationale=rationale)


@dataclass(frozen=True, slots=True)
class ObservabilitySettings:
    """Freshness horizons, and the bounds on what a health read costs."""

    fresh_within_hours: ObservabilitySetting = field(
        default_factory=lambda: _s(
            26.0,
            "Newer than this and a feed is FRESH. Twenty-six hours rather "
            "than twenty-four so a Tuesday morning read of Monday's close "
            "is fresh rather than borderline — a threshold that fires "
            "every morning is a threshold nobody reads.",
        )
    )
    delayed_within_hours: ObservabilitySetting = field(
        default_factory=lambda: _s(
            80.0,
            "Between fresh and this, a feed is DELAYED: late but explicable "
            "by a weekend. Friday's close read on Monday morning is about "
            "62 hours old and is not a problem; 80 hours covers a long "
            "weekend without covering a genuine outage.",
        )
    )
    scan_run_stuck_after_hours: ObservabilitySetting = field(
        default_factory=lambda: _s(
            6.0,
            "A RUNNING scan with no finished_at older than this is treated "
            "as a died process. Module 18's own phrasing — 'a RUNNING row "
            "with no finished_at after the window means a process died' — "
            "with a number attached.",
        )
    )
    anomaly_window_days: ObservabilitySetting = field(
        default_factory=lambda: _s(
            30.0,
            "How far back the anomaly monitors count. Long enough that a "
            "state-machine gap appearing a few times a month is visible, "
            "short enough that a fixed problem stops being reported.",
        )
    )
    max_rows_sampled: ObservabilitySetting = field(
        default_factory=lambda: _s(
            500.0,
            "Ceiling on rows any single health read returns. A health check "
            "that gets slow under load is a health check that fails exactly "
            "when it is needed.",
        )
    )

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name).value) for f in fields(self)}

    def describe(self) -> dict[str, dict[str, Any]]:
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @property
    def fresh_within_seconds(self) -> float:
        return self.fresh_within_hours.value * 3600.0

    @property
    def delayed_within_seconds(self) -> float:
        return self.delayed_within_hours.value * 3600.0

    @property
    def scan_run_stuck_after_seconds(self) -> float:
        return self.scan_run_stuck_after_hours.value * 3600.0


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    name: str = "argus-observability"
    settings: ObservabilitySettings = field(default_factory=ObservabilitySettings)

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "settings": self.settings.as_dict()}

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"
