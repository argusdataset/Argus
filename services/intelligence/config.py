"""The two numbers this module has, isolated per Module 10's discipline.

Almost nothing here, and that is the honest state: this module computes
no statistic and decides no threshold. What it has is a page bound and a
staleness horizon, both operational — they change how much of an answer
is returned and when a response calls itself old, never what any answer
says.
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
    "IntelligenceConfig",
    "IntelligenceSetting",
    "IntelligenceSettings",
]


@dataclass(frozen=True, slots=True)
class IntelligenceSetting:
    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value

    def __int__(self) -> int:
        return int(self.value)


def _s(value: float, kind: str, rationale: str) -> IntelligenceSetting:
    return IntelligenceSetting(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class IntelligenceSettings:
    """A page bound and a staleness horizon. Nothing else."""

    max_watchlist_entries: IntelligenceSetting = field(
        default_factory=lambda: _s(
            200.0,
            OPERATIONAL,
            "Entries returned in one watchlist request. The full membership "
            "count is reported regardless, so a client showing the first "
            "page knows how many there are — this bounds the payload, not "
            "the list. Operational because the same securities are on the "
            "list either way.",
        )
    )
    stale_after_hours: IntelligenceSetting = field(
        default_factory=lambda: _s(
            36.0,
            OPERATIONAL,
            "How old a stored signal may be before a response describes "
            "itself as stale. A day and a half rather than a day: Module "
            "18 scans once per trading day, so a Monday response about "
            "Friday's scan is normal and calling it stale would be crying "
            "wolf over a weekend. Reported, never enforced — a stale answer "
            "is still served, with its age attached.",
        )
    )

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

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @property
    def stale_after_seconds(self) -> float:
        return self.stale_after_hours.value * 3600.0


@dataclass(frozen=True, slots=True)
class IntelligenceConfig:
    name: str = "argus-intelligence"
    settings: IntelligenceSettings = field(default_factory=IntelligenceSettings)
    #: Trust `X-Argus-User` as identity, exactly as `TerminalConfig`'s
    #: field of the same name does.
    #:
    #: It exists here because `app.py` used to construct a
    #: `TerminalConfig()` inline, which meant this service trusted the
    #: header and no caller could turn that off — not even the deployment
    #: that owns the exposure decision. Real sessions are accepted either
    #: way; this gates only the header. `infra/deploy/asgi.py` derives it
    #: from the deployment profile, and staging and production forbid it.
    stub_identity_enabled: bool = True

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "settings": self.settings.as_dict(),
            "stub_identity_enabled": self.stub_identity_enabled,
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"
