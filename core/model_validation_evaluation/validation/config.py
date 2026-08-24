"""Every number the replay engine uses, isolated per Module 10's discipline.

**None of the calibratable values here has been validated**, same status as
Modules 10-15 and for the same reason — which is slightly circular in this
module's case, because this module *is* the thing that produces the
evidence to validate them with. The circularity is real and worth stating:
the replay cadence below decides how much evidence a run yields, and it was
chosen before any of that evidence existed.

## A third kind, and why

Modules 10-15 tag every threshold `structural` (follows from a definition;
changing it changes what the number means) or `calibratable` (an invented
magnitude; recalibration starts here). Neither fits a batch chunk size.

So this module adds **`operational`**: a number that bounds *how* the
computation runs and provably cannot change *what* it produces. The proof
obligation is not rhetorical — `tests/unit/model_validation_evaluation/`
runs the same replay at two chunk sizes and asserts the outputs are
identical. A constant that fails that test is not operational; it is a
calibratable threshold that was mislabelled, and the test says so.
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
#: Bounds how the computation runs, never what it produces.
OPERATIONAL = "operational"

KINDS = (STRUCTURAL, CALIBRATABLE, OPERATIONAL)


@dataclass(frozen=True, slots=True)
class ValidationSetting:
    """One named number, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _s(value: float, kind: str, rationale: str) -> ValidationSetting:
    return ValidationSetting(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class ReplaySettings:
    """How the replay walks history."""

    scan_step_days: ValidationSetting = field(
        default_factory=lambda: _s(
            5.0,
            CALIBRATABLE,
            "Trading days between replay scan dates. Not daily, because a "
            "base does not change materially overnight and a daily walk "
            "costs five times the compute for near-duplicate observations; "
            "not monthly, because a breakout can complete inside a month "
            "and ARGUS would only ever see it afterwards. A week is a "
            "guess at that trade-off, and it is a guess that changes how "
            "much evidence a run produces — which makes it the first "
            "thing to revisit once a real run has happened.",
        )
    )
    batch_size: ValidationSetting = field(
        default_factory=lambda: _s(
            2000.0,
            OPERATIONAL,
            "Securities per feature-computation chunk. A full universe is "
            "~10,000 names and the panel is a dense (dates x securities) "
            "float frame over the maximum lookback, so one unchunked pass "
            "is a several-gigabyte allocation. Chunking bounds peak "
            "memory and provably nothing else: features are computed per "
            "security from that security's own history plus shared "
            "benchmarks, so no cross-security statistic spans a chunk "
            "boundary. Detection's ranks *are* cross-sectional, which is "
            "why ranking happens once over the merged result rather than "
            "per chunk.",
        )
    )
    max_scan_dates: ValidationSetting = field(
        default_factory=lambda: _s(
            10000.0,
            OPERATIONAL,
            "A refusal bound, not a tuning knob. Thirty years at a weekly "
            "cadence is roughly 1,560 dates; ten thousand means the caller "
            "asked for something other than what they meant (a reversed "
            "period, a one-day step across decades) and should hear about "
            "it before the run rather than after.",
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
        return {
            f.name: float(getattr(self, f.name).value)
            for f in fields(self)
            if getattr(self, f.name).kind == CALIBRATABLE
        }

    def operational(self) -> dict[str, float]:
        """The numbers a test must prove cannot change a result."""
        return {
            f.name: float(getattr(self, f.name).value)
            for f in fields(self)
            if getattr(self, f.name).kind == OPERATIONAL
        }

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> ReplaySettings:
        stored = definition.get("settings", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = ValidationSetting(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)

    @property
    def step(self) -> timedelta:
        return timedelta(days=self.scan_step_days.value)

    @property
    def chunk(self) -> int:
        return int(self.batch_size.value)


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    """The replay engine's complete, versioned configuration."""

    name: str = "argus-validation"
    settings: ReplaySettings = field(default_factory=ReplaySettings)

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "settings": self.settings.as_dict(),
            "calibration_status": "UNVALIDATED_PLACEHOLDERS",
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"
