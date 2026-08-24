"""Every number the scanner uses, isolated per Module 10's discipline.

Reuses Module 17's three kinds — `structural`, `calibratable`,
`operational` — rather than inventing a fourth. Most of what is here is
operational, which is a first for this project and is the honest tag:
these numbers bound *how* the scanner runs and none of them can change
what a scan computes. A scan started at 21:00 and a scan started at 23:00
produce identical signals, because `as_of` is derived from the session
close, not from when the process happened to wake up.

The one exception is `min_universe_coverage`, and it is calibratable in
the strict sense: it decides whether a day gets scanned at all, and
setting it wrong either scans a half-empty universe or waits forever.

## The timing decision, and why it is designed to tolerate being wrong

`scan_offset_hours` says how long after the session close to first ask
whether the data is ready. Five hours (21:00 ET) is a guess, and it is a
guess about somebody else's delivery schedule, which is not a thing to
guess about confidently.

So the schedule is not the gate. The **readiness check** is: the scanner
asks whether the data is actually there, and if it is not, records
DATA_NOT_READY and asks again `retry_interval_minutes` later, up to
`readiness_window_hours`. Being wrong about the offset therefore costs a
retry, not a missed day — which is the property worth designing for,
because the alternative is a scanner whose correctness depends on a
provider's SLA staying what it was.
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
    "ScannerConfig",
    "ScannerSetting",
    "ScannerSettings",
]


@dataclass(frozen=True, slots=True)
class ScannerSetting:
    """One named number, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _s(value: float, kind: str, rationale: str) -> ScannerSetting:
    return ScannerSetting(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class ScannerSettings:
    """When to scan, how long to wait, and when to give up."""

    # -- Scheduling ---------------------------------------------------------
    scan_offset_hours: ScannerSetting = field(
        default_factory=lambda: _s(
            5.0,
            OPERATIONAL,
            "Hours after the 16:00 ET session close before the scanner "
            "first asks whether the day's data has arrived — 21:00 ET. A "
            "guess about a provider's delivery schedule, which is why the "
            "readiness check rather than this number is the actual gate: "
            "being wrong here costs a retry, not a missed day. Operational "
            "because `as_of` is derived from the session close, so a scan "
            "started at 21:00 and one started at 23:00 compute identically.",
        )
    )
    retry_interval_minutes: ScannerSetting = field(
        default_factory=lambda: _s(
            30.0,
            OPERATIONAL,
            "How long to wait before asking again after DATA_NOT_READY. "
            "Half an hour is short enough that a late feed costs at most "
            "that much delay, and long enough that a whole evening of "
            "waiting is a manageable number of attempts rather than "
            "hundreds of rows in live_scan_runs.",
        )
    )
    readiness_window_hours: ScannerSetting = field(
        default_factory=lambda: _s(
            12.0,
            OPERATIONAL,
            "How long after the session close the scanner keeps waiting "
            "for data before the day stops being 'not ready yet'. Twelve "
            "hours reaches 04:00 ET the next morning; past that the data "
            "is not late, something is wrong, and continuing to record "
            "DATA_NOT_READY would be the scanner going quietly dark while "
            "looking busy.",
        )
    )

    # -- Readiness ----------------------------------------------------------
    min_universe_coverage: ScannerSetting = field(
        default_factory=lambda: _s(
            0.80,
            CALIBRATABLE,
            "Fraction of the universe that must have a bar for the scan "
            "date before the day is considered scannable. Not 1.0: on any "
            "real day some names are halted, some are newly listed, and "
            "some the provider simply does not deliver, so demanding "
            "everything would mean never scanning. Not 0.5 either, because "
            "Module 09's ranking is cross-sectional — a candidate pool "
            "chosen from half the universe is a different pool, not a "
            "smaller one. 80% is a guess at that boundary and it is the "
            "one number here that decides whether a day gets scanned.",
        )
    )

    # -- Transient failure --------------------------------------------------
    max_attempts: ScannerSetting = field(
        default_factory=lambda: _s(
            4.0,
            OPERATIONAL,
            "Attempts at one scan date before it is recorded FAILED. Four "
            "with exponential backoff spans roughly a minute of retrying, "
            "which covers a database restart or a brief network partition "
            "and does not cover a real outage — the two need different "
            "responses and only one of them is this module's to give.",
        )
    )
    backoff_base_seconds: ScannerSetting = field(
        default_factory=lambda: _s(
            2.0,
            OPERATIONAL,
            "First backoff interval, doubling each attempt. Exponential "
            "rather than fixed so a transient blip clears fast and a "
            "genuine outage is not hammered.",
        )
    )
    backoff_max_seconds: ScannerSetting = field(
        default_factory=lambda: _s(
            60.0,
            OPERATIONAL,
            "Ceiling on the backoff. Past a minute the scanner is not "
            "recovering from a blip any more and should stop, record "
            "FAILED, and let the next scheduled run try afresh.",
        )
    )

    # -- Per-security isolation ---------------------------------------------
    max_excluded_fraction: ScannerSetting = field(
        default_factory=lambda: _s(
            0.05,
            CALIBRATABLE,
            "How much of the universe may be quarantined before the day is "
            "a failure rather than a scan with exclusions. Isolating one "
            "bad security to save the day is the point; isolating a third "
            "of the universe and reporting success is how a broken feed "
            "gets recorded as a normal Tuesday. Five percent is a guess at "
            "where 'a few bad names' becomes 'something is wrong'.",
        )
    )

    min_excluded_allowance: ScannerSetting = field(
        default_factory=lambda: _s(
            1.0,
            STRUCTURAL,
            "Exclusions always permitted regardless of the fraction. One "
            "bad security is never a broken feed — that is what the "
            "fraction guards against — and without this floor the budget "
            "would make a single bad name un-isolatable in any universe "
            "smaller than 1/max_excluded_fraction, which is precisely the "
            "case per-security isolation exists for. Structural because "
            "it follows from what the fraction means rather than being a "
            "magnitude anyone would tune.",
        )
    )

    # -- What gets written down ---------------------------------------------
    max_stored_message_chars: ScannerSetting = field(
        default_factory=lambda: _s(
            2000.0,
            OPERATIONAL,
            "How much of an exception's text is kept in a run's `detail`. "
            "A JSONB column is not the place for a megabyte of pandas "
            "repr, and the first two thousand characters carry the type, "
            "the message and the useful part of the context. One number "
            "for every truncation in the module rather than three "
            "different arbitrary ones, which is what a first draft had.",
        )
    )
    max_listed_exclusions: ScannerSetting = field(
        default_factory=lambda: _s(
            5.0,
            OPERATIONAL,
            "How many quarantined securities the human-readable note names "
            "before it stops listing them. The full list is always in "
            "`excluded_securities`; this only bounds the one-line summary.",
        )
    )

    # -- Catch-up -----------------------------------------------------------
    max_catchup_days: ScannerSetting = field(
        default_factory=lambda: _s(
            30.0,
            OPERATIONAL,
            "How far back catch-up will look for unscanned trading days. "
            "This is the number that keeps 'incremental, never re-scans "
            "full history' an operational promise rather than an "
            "architectural intention: a misconfigured date range cannot "
            "walk the scanner back through fifteen years, because catch-up "
            "will not look further than this and says so when it stops.",
        )
    )
    min_attempt_number: ScannerSetting = field(
        default_factory=lambda: _s(
            0.0,
            STRUCTURAL,
            "The first attempt at a date is attempt 0. Not a knob: the "
            "number is an index into that date's attempts, and starting "
            "anywhere else would make 'how many times did this fail' wrong "
            "by a constant.",
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
    def from_definition(cls, definition: dict[str, Any]) -> ScannerSettings:
        stored = definition.get("settings", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = ScannerSetting(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)

    @property
    def scan_offset(self) -> timedelta:
        return timedelta(hours=self.scan_offset_hours.value)

    @property
    def retry_interval(self) -> timedelta:
        return timedelta(minutes=self.retry_interval_minutes.value)

    @property
    def readiness_window(self) -> timedelta:
        return timedelta(hours=self.readiness_window_hours.value)

    @property
    def attempts(self) -> int:
        return int(self.max_attempts.value)

    @property
    def catchup_window(self) -> int:
        return int(self.max_catchup_days.value)

    @property
    def exclusion_allowance(self) -> int:
        return int(self.min_excluded_allowance.value)

    @property
    def message_limit(self) -> int:
        return int(self.max_stored_message_chars.value)

    @property
    def listed_exclusions(self) -> int:
        return int(self.max_listed_exclusions.value)

    def backoff_for(self, attempt: int) -> float:
        """Seconds to wait before `attempt`, doubling and then capped."""
        base = self.backoff_base_seconds.value
        ceiling = self.backoff_max_seconds.value
        return min(base * (2 ** max(attempt, 0)), ceiling)


@dataclass(frozen=True, slots=True)
class ScannerConfig:
    """The scanner's complete, versioned configuration."""

    name: str = "argus-live-scanner"
    settings: ScannerSettings = field(default_factory=ScannerSettings)

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
