"""Every number the evaluation uses, isolated per Module 10's discipline.

The same three kinds as `validation/config.py` — `structural`,
`calibratable`, `operational`.

## What is not here, and why that matters more than what is

There is no success threshold in this file. Whether a setup succeeded is
Module 15's judgement, recorded as `outcome_status` on the row, computed
under a named `data_snapshot`. Re-deciding it here — "call it a win if the
return beat 8%" — would produce metrics that disagree with the stored
dataset while looking identical to it, and the disagreement would be
invisible because both numbers would be called a hit rate.

So evaluation reads labels and never assigns them. If the success
criterion is wrong, the fix is a new snapshot and a re-run of Module 15
(which migration 0007 exists to make possible), not a threshold here.

## Sample floors are the honesty mechanism

Most of the calibratable numbers below are minimum sample sizes. They are
what stops this module reporting "92% hit rate in the 80-90 bucket" from
twelve setups. Module 11 established the pattern — statistics are `None`
below the floor, not approximate — and every breakdown here carries a
`SampleSufficiency` for the same reason.
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
    "EvaluationConfig",
    "EvaluationThreshold",
    "EvaluationThresholds",
    "ScoreBucketing",
]


@dataclass(frozen=True, slots=True)
class EvaluationThreshold:
    """One named number, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _t(value: float, kind: str, rationale: str) -> EvaluationThreshold:
    return EvaluationThreshold(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class ScoreBucketing:
    """The score bands outcomes are reported against.

    A tuple rather than a float, so it does not fit the threshold shape —
    but it is a calibration parameter in every other sense, so it carries
    the same kind tag and rationale and appears in the published
    definition.
    """

    edges: tuple[float, ...] = (20.0, 40.0, 60.0, 70.0, 80.0, 90.0, 100.0)
    kind: str = CALIBRATABLE
    rationale: str = (
        "The project's stated buckets (20-40, 40-60, 60-70, 70-80, 80-90, "
        "90-100), narrowing as the score rises because that is where the "
        "distinction matters and where ARGUS acts. Chosen before any score "
        "distribution existed to choose against: if a real run puts 95% of "
        "setups in one band, these are the wrong edges and the run itself "
        "will say so."
    )

    #: The full domain `argus_score` is defined on. Separate from `edges`
    #: because the buckets deliberately start at 20 — nothing below it is
    #: expected to reach evaluation — while the calibration curve spans
    #: the whole range, so an unexpectedly low score is visible rather
    #: than off the chart.
    score_range: tuple[float, float] = (0.0, 100.0)
    score_range_kind: str = STRUCTURAL
    score_range_rationale: str = (
        "Module 13's composite is defined on 0-100 by construction: the "
        "components are scaled to it and the weights sum to one. Changing "
        "this would not recalibrate anything, it would mean a different "
        "score — which is what makes it structural rather than a knob."
    )

    def labels(self) -> tuple[str, ...]:
        return tuple(
            f"{int(low)}-{int(high)}" for low, high in zip(self.edges, self.edges[1:], strict=False)
        )

    def describe(self) -> dict[str, dict[str, Any]]:
        """Both entries, each with its own kind — they are not the same kind."""
        return {
            "edges": {
                "value": list(self.edges),
                "kind": self.kind,
                "rationale": self.rationale,
            },
            "score_range": {
                "value": list(self.score_range),
                "kind": self.score_range_kind,
                "rationale": self.score_range_rationale,
            },
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "edges": list(self.edges),
            "kind": self.kind,
            "rationale": self.rationale,
            "score_range": list(self.score_range),
        }


@dataclass(frozen=True, slots=True)
class EvaluationThresholds:
    """The sample floors and window sizes evaluation runs under."""

    # -- Sample floors ------------------------------------------------------
    min_bucket_sample: EvaluationThreshold = field(
        default_factory=lambda: _t(
            20.0,
            CALIBRATABLE,
            "Setups a score bucket needs before its statistics are reported "
            "at all. Matches the spirit of Module 11's analogue floor: "
            "below it the bucket reports a count and INSUFFICIENT, never a "
            "hit rate. A monotonicity claim built on twelve setups in the "
            "top bucket is the single most likely way this system talks "
            "itself into believing its own score works.",
        )
    )
    min_regime_sample: EvaluationThreshold = field(
        default_factory=lambda: _t(
            20.0,
            CALIBRATABLE,
            "Same floor for a regime breakdown. Deliberately a separate "
            "number even though it currently equals the bucket floor: the "
            "two answer different questions and there is no reason they "
            "should move together when either is recalibrated.",
        )
    )
    min_calibration_bin_sample: EvaluationThreshold = field(
        default_factory=lambda: _t(
            10.0,
            CALIBRATABLE,
            "Setups a calibration bin needs to report an observed rate. "
            "Lower than the bucket floor on purpose — a calibration curve "
            "is read as a shape across bins, where a thin bin is visibly "
            "thin, rather than as a single quoted number.",
        )
    )

    # -- Monotonicity -------------------------------------------------------
    min_buckets_for_monotonicity: EvaluationThreshold = field(
        default_factory=lambda: _t(
            2.0,
            STRUCTURAL,
            "You cannot observe a trend in one bucket. Not a tuning knob: "
            "below two reportable buckets the monotonicity question has no "
            "answer, and the analysis says so instead of returning a "
            "vacuous pass.",
        )
    )
    monotonicity_tolerance: EvaluationThreshold = field(
        default_factory=lambda: _t(
            0.02,
            CALIBRATABLE,
            "How far a bucket's outcome may fall below the bucket beneath "
            "it before that counts as a violation rather than noise. Two "
            "percentage points of benchmark-relative return is a guess at "
            "the width of the noise band, and it is the number that "
            "decides whether a wobbly-but-rising curve reads as a pass or "
            "a failure — which makes it worth recalibrating early.",
        )
    )

    # -- Walk-forward -------------------------------------------------------
    walk_forward_train_days: EvaluationThreshold = field(
        default_factory=lambda: _t(
            730.0,
            CALIBRATABLE,
            "Observation window per walk-forward roll. Two years is a guess "
            "at 'long enough to contain a full base-to-expansion cycle for "
            "a reasonable number of names'. ARGUS does not fit parameters "
            "on this window today — nothing is trained — so it is currently "
            "the period whose evidence a recalibration *would* use.",
        )
    )
    walk_forward_test_days: EvaluationThreshold = field(
        default_factory=lambda: _t(
            365.0,
            CALIBRATABLE,
            "Out-of-sample window per roll. One year, so a roll's test "
            "period is long enough to contain outcomes at Module 15's "
            "60-day horizon several times over without being long enough to "
            "average across several different market regimes.",
        )
    )
    walk_forward_step_days: EvaluationThreshold = field(
        default_factory=lambda: _t(
            365.0,
            CALIBRATABLE,
            "How far each roll advances. Equal to the test window, so "
            "consecutive test periods tile history rather than overlapping "
            "— overlapping tests would count the same outcome in several "
            "rolls and make the rolls look more independent than they are.",
        )
    )
    min_walk_forward_windows: EvaluationThreshold = field(
        default_factory=lambda: _t(
            2.0,
            STRUCTURAL,
            "One window is a train/test split, not a walk-forward. The "
            "whole point is comparing performance across rolls, so below "
            "two the analysis reports that the period was too short rather "
            "than reporting one window's numbers as if they were a result.",
        )
    )

    # -- Reporting ----------------------------------------------------------
    calibration_bins: EvaluationThreshold = field(
        default_factory=lambda: _t(
            10.0,
            OPERATIONAL,
            "Bins across the 0-100 score range for the calibration curve. "
            "A display granularity: changing it changes how finely the same "
            "underlying outcomes are grouped for reading, and no metric "
            "computed anywhere else reads it.",
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
    def from_definition(cls, definition: dict[str, Any]) -> EvaluationThresholds:
        stored = definition.get("thresholds", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = EvaluationThreshold(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)

    @property
    def train_window(self) -> timedelta:
        return timedelta(days=self.walk_forward_train_days.value)

    @property
    def test_window(self) -> timedelta:
        return timedelta(days=self.walk_forward_test_days.value)

    @property
    def roll_step(self) -> timedelta:
        return timedelta(days=self.walk_forward_step_days.value)


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    """The evaluation's complete, versioned configuration."""

    name: str = "argus-evaluation"
    thresholds: EvaluationThresholds = field(default_factory=EvaluationThresholds)
    buckets: ScoreBucketing = field(default_factory=ScoreBucketing)

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "thresholds": self.thresholds.as_dict(),
            "buckets": self.buckets.as_dict(),
            "calibration_status": "UNVALIDATED_PLACEHOLDERS",
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"
