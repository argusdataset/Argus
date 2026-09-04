"""Every number this module uses, isolated per Module 10's discipline.

**None of these values has been validated.** Same status as Modules 10,
11 and 12: there is no historical outcome data yet to say whether "three
times the trailing average" is the right multiple, or whether thirty days
is the right baseline window. Every result this module produces carries
`calibration_status: "UNVALIDATED_PLACEHOLDERS"`.

## Kind tagging

Reuses Module 12's two kinds rather than inventing a third:

- **`structural`** — follows from how something is defined. Changing it
  changes what the number *means*.
- **`calibratable`** — an invented magnitude. Recalibration starts here.

## Why the anomaly threshold has no absolute floor

The obvious defensive addition — "require at least N articles before
raising, even if that is far above the baseline multiple" — was
deliberately not made. This project already ran that argument once, the
other way: Module 15's original success criterion was a flat percentage
across a heterogeneous universe, and the audit that replaced it with an
ATR-relative threshold is recorded in `docs/architecture/KNOWN_ISSUES.md`
E5. A flat article-count floor here would reintroduce exactly that
mistake in the opposite direction — silencing the signal for a
name that normally has *some* baseline chatter, in favour of one that
happens to clear a fixed count. The threshold stays purely relative to
each security's own baseline, at whatever the multiple implies.

The one honest consequence, stated rather than patched: a security whose
baseline is genuinely zero raises on its very first article. That is not
a bug — a name that normally gets no coverage getting any coverage at all
*is* unusual for that name — but it is worth a reader knowing the rule
produces it deliberately rather than discovering it by surprise.
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

KINDS = (STRUCTURAL, CALIBRATABLE)

__all__ = [
    "CALIBRATABLE",
    "KINDS",
    "STRUCTURAL",
    "FilingIngestionSettings",
    "NewsSignalConfig",
    "NewsSignalThreshold",
    "NewsSignalThresholds",
]


@dataclass(frozen=True, slots=True)
class NewsSignalThreshold:
    """One named number, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _t(value: float, kind: str, rationale: str) -> NewsSignalThreshold:
    return NewsSignalThreshold(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class NewsSignalThresholds:
    """The complete threshold set. Enumerable as a whole."""

    baseline_window_days: NewsSignalThreshold = field(
        default_factory=lambda: _t(
            30.0,
            CALIBRATABLE,
            "How many trailing calendar days form the 'normal' volume a "
            "security is compared against. A guess at the shortest window "
            "long enough to smooth over an ordinary quiet week without "
            "being so long that it absorbs a genuine regime change in how "
            "much this name gets covered.",
        )
    )
    anomaly_multiple: NewsSignalThreshold = field(
        default_factory=lambda: _t(
            3.0,
            CALIBRATABLE,
            "How many times the trailing daily average today's count must "
            "reach before the signal raises. A guess at 'clearly more than "
            "noise' — nothing has measured what multiple actually "
            "separates an ordinary busy day from a genuine anomaly for "
            "this kind of count.",
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

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> NewsSignalThresholds:
        stored = definition.get("thresholds", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = NewsSignalThreshold(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)

    @property
    def window_days(self) -> int:
        return int(self.baseline_window_days.value)

    @property
    def window(self) -> timedelta:
        return timedelta(days=self.window_days)

    @property
    def multiple(self) -> float:
        return self.anomaly_multiple.value


@dataclass(frozen=True, slots=True)
class FilingIngestionSettings:
    """The one number the SEC-filing ingestion path needs: how long after
    a filing's accepted timestamp ARGUS could plausibly have fetched it.

    Kept separate from `NewsSignalThresholds` deliberately: this is a
    PIT-timing assumption about data latency, not a calibratable decision
    about what counts as an anomaly, and mixing the two would make
    `NewsSignalThresholds.calibratable()` claim a data-latency constant is
    an invented magnitude waiting on outcome data, which it is not —
    mirroring the same separation `data/canonical_model/pit.py`'s
    `ProviderLagPolicy` draws from every calibratable threshold in this
    project.
    """

    availability_lag_hours: NewsSignalThreshold = field(
        default_factory=lambda: _t(
            24.0,
            STRUCTURAL,
            "Hours after a filing's accepted timestamp before ARGUS could "
            "plausibly have fetched it — the same 24-hour assumption "
            "`ProviderLagPolicy.fundamentals` makes for SEC-accepted "
            "filings generally. Structural: it follows from how quickly "
            "an aggregator can be expected to surface a new filing, not "
            "from a trading judgement.",
        )
    )

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name).value) for f in fields(self)}

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @property
    def availability_lag(self) -> timedelta:
        return timedelta(hours=self.availability_lag_hours.value)


@dataclass(frozen=True, slots=True)
class NewsSignalConfig:
    """The module's complete, versioned configuration."""

    name: str = "argus-news-signals"
    thresholds: NewsSignalThresholds = field(default_factory=NewsSignalThresholds)

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
