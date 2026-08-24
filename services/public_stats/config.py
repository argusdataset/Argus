"""Every number the public page is shaped by, isolated per Module 10's discipline.

Reuses Module 17's three kinds. Almost everything here is `operational` —
a histogram bin count changes how outcomes are grouped for reading, never
what any of them were.

## The one that is not

`min_public_sample` is `calibratable`, and it is the most consequential
number in this module. It decides how much evidence ARGUS requires before
it will show a statistic to a stranger rather than showing a count and an
explanation.

It deliberately does **not** default to Module 17's floor. Module 17's
`min_bucket_sample` governs an internal analysis a person reads knowing
its limits; this governs a public page read by someone who has no way to
know them. The public floor should be at least as strict, never looser,
and a test asserts that relationship rather than trusting the two numbers
to be kept in step by hand.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import timedelta
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
    "PublicStatsConfig",
    "PublicStatsSetting",
    "PublicStatsSettings",
]


@dataclass(frozen=True, slots=True)
class PublicStatsSetting:
    """One named number, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value

    def __int__(self) -> int:
        return int(self.value)


def _s(value: float, kind: str, rationale: str) -> PublicStatsSetting:
    return PublicStatsSetting(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class PublicStatsSettings:
    """Sample floors, chart shapes, and the cache's expected age."""

    # -- The floor that decides what a stranger is shown ---------------------
    min_public_sample: PublicStatsSetting = field(
        default_factory=lambda: _s(
            30.0,
            CALIBRATABLE,
            "Outcomes a bucket needs before its rate is shown publicly. "
            "Stricter than Module 17's internal floor of 20 on purpose: an "
            "analyst reading an evaluation report knows what a thin sample "
            "means, and a stranger reading a percentage on a public page "
            "does not. Below this the chart carries a count and an "
            "explanation instead of a number — which looks worse and is "
            "the point.",
        )
    )

    # -- Chart shapes --------------------------------------------------------
    excursion_bins: PublicStatsSetting = field(
        default_factory=lambda: _s(
            12.0,
            OPERATIONAL,
            "Histogram bins across the MFE/MAE range. A display "
            "granularity: the same excursions, grouped more or less "
            "finely for reading. Nothing computed anywhere else reads it.",
        )
    )
    excursion_clip: PublicStatsSetting = field(
        default_factory=lambda: _s(
            2.0,
            OPERATIONAL,
            "Excursion magnitude at which the outermost histogram bin "
            "becomes open-ended (+200% / -200%). Not a filter — every "
            "outcome is counted, extremes land in the edge bins — but "
            "without it one 40x mover stretches the axis until every "
            "other bar is invisible.",
        )
    )
    cumulative_max_points: PublicStatsSetting = field(
        default_factory=lambda: _s(
            2000.0,
            OPERATIONAL,
            "Points in the cumulative-performance series before it is "
            "thinned. A line chart cannot render more than a couple of "
            "thousand distinguishable points, and shipping 200,000 to a "
            "browser is bandwidth spent on pixels nobody sees. Thinning "
            "keeps the endpoints, so the final cumulative figure is exact.",
        )
    )

    # -- Cache ---------------------------------------------------------------
    expected_refresh_hours: PublicStatsSetting = field(
        default_factory=lambda: _s(
            24.0,
            OPERATIONAL,
            "How old a snapshot may be before responses describe it as "
            "stale. Not an expiry — a stale snapshot is still served, "
            "because a day-old true number beats a spinner — but the age "
            "is reported so a reader can judge it. Daily matches the "
            "cadence at which outcomes actually arrive: Module 18 scans "
            "once per trading day.",
        )
    )

    # -- Structural ----------------------------------------------------------
    min_points_for_series: PublicStatsSetting = field(
        default_factory=lambda: _s(
            2.0,
            STRUCTURAL,
            "A line needs two points. Not a knob: one point is not a "
            "trend, and drawing it as one would be the chart lying about "
            "what it knows.",
        )
    )

    # ---------------------------------------------------------------------
    # Whole-set access.
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

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @property
    def refresh_interval(self) -> timedelta:
        return timedelta(hours=self.expected_refresh_hours.value)


@dataclass(frozen=True, slots=True)
class PublicStatsConfig:
    """The public page's complete, versioned configuration."""

    name: str = "argus-public-stats"
    settings: PublicStatsSettings = field(default_factory=PublicStatsSettings)

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "settings": self.settings.as_dict()}

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"
