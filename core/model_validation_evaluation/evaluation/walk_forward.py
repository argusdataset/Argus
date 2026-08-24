"""Walk-forward: does performance hold across regimes, or was one stretch kind?

## The failure mode this exists to catch

A single in-sample/out-of-sample split has one test period. If that period
happened to be 2013 or 2021, a model that only works in a rising market
looks excellent, and there is nothing in the number to say so. Rolling the
split forward repeatedly turns "it worked" into "it worked in these years
and not those", which is the only form of the claim worth acting on.

## What is and is not being trained

Nothing is trained. ARGUS's weights and thresholds are hand-chosen
placeholders (Modules 10-15 all say so in their stored configuration), so
each roll's train window is not fitting anything — the same configuration
runs in both halves of every window.

That makes the current output an **observation-period / test-period**
comparison rather than a true out-of-sample test, and this module says so
in its own report rather than letting the name imply otherwise. The
mechanism is built now, with the windows and the roll arithmetic correct,
so that the day weights are actually fitted per window the only change is
what happens between `train` and `test` — not the surrounding structure.

## Stability is the output, not an average

Averaging the rolls would reproduce exactly the problem walk-forward
exists to expose. So the analysis reports each roll separately, plus the
spread across rolls (`expectancy_range`, `consistent_direction`), and a
`stable` verdict that requires every reportable roll to point the same
way. Two good years and one catastrophic one is not a positive average;
it is an unstable model, and it reads as one here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from core.model_validation_evaluation.evaluation.config import EvaluationConfig
from core.model_validation_evaluation.evaluation.dataset import EvaluationDataset
from core.model_validation_evaluation.evaluation.metrics import MetricSuite, compute_metrics


@dataclass(frozen=True, slots=True)
class Window:
    """One roll's two periods. Non-overlapping by construction."""

    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime

    def as_dict(self) -> dict[str, str | int]:
        return {
            "index": self.index,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Roll:
    """A window and what happened in each of its halves."""

    window: Window
    train: MetricSuite
    test: MetricSuite

    @property
    def reportable(self) -> bool:
        return self.test.reportable and self.test.expectancy is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window.as_dict(),
            "train": self.train.as_dict(),
            "test": self.test.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class WalkForwardAnalysis:
    """Every roll, and whether the model held its shape across them."""

    rolls: list[Roll]
    reportable_rolls: int
    stable: bool | None
    consistent_direction: bool | None
    expectancy_range: tuple[float, float] | None
    mean_test_expectancy: float | None
    undetermined_reason: str | None = None
    #: Stated on every report so the name is never read as more than it is.
    caveat: str = (
        "No parameters are fitted per window — ARGUS's weights and thresholds are "
        "fixed placeholders — so each roll compares an observation period against a "
        "later period under identical configuration. The mechanism is a true "
        "walk-forward; the current runs are not out-of-sample in the fitted sense."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rolls": [roll.as_dict() for roll in self.rolls],
            "reportable_rolls": self.reportable_rolls,
            "stable": self.stable,
            "consistent_direction": self.consistent_direction,
            "expectancy_range": list(self.expectancy_range) if self.expectancy_range else None,
            "mean_test_expectancy": self.mean_test_expectancy,
            "undetermined_reason": self.undetermined_reason,
            "caveat": self.caveat,
        }

    def summary(self) -> str:
        if self.stable is None:
            return f"Walk-forward undetermined: {self.undetermined_reason}"
        verdict = "stable" if self.stable else "unstable"
        low, high = self.expectancy_range or (0.0, 0.0)
        return (
            f"Walk-forward {verdict} across {self.reportable_rolls} reportable roll(s); "
            f"test expectancy ranged {low:+.4f} to {high:+.4f}."
        )


def build_windows(
    period_start: datetime,
    period_end: datetime,
    config: EvaluationConfig | None = None,
) -> list[Window]:
    """Rolling train/test windows across the period.

    Each roll advances by `walk_forward_step_days`, which defaults to the
    test length so consecutive test periods tile rather than overlap. A
    window whose test period would run past `period_end` is not emitted:
    a truncated final window would be measured on less data than the
    others and then compared against them as an equal.
    """
    config = config or EvaluationConfig()
    thresholds = config.thresholds
    train = thresholds.train_window
    test = thresholds.test_window
    step = max(thresholds.roll_step, timedelta(days=1))

    windows: list[Window] = []
    train_start = period_start
    index = 0
    while True:
        train_end = train_start + train
        test_start = train_end
        test_end = test_start + test
        if test_end > period_end:
            break
        windows.append(
            Window(
                index=index,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        index += 1
        train_start = train_start + step
    return windows


def walk_forward(
    dataset: EvaluationDataset,
    *,
    period_start: datetime,
    period_end: datetime,
    config: EvaluationConfig | None = None,
) -> WalkForwardAnalysis:
    """Roll the split forward and report each window separately.

    Slices one already-loaded dataset per window rather than re-querying:
    `EvaluationDataset.within` exists for this, and at real scale the
    difference is one pass over the dataset instead of one query per roll.
    """
    config = config or EvaluationConfig()
    windows = build_windows(period_start, period_end, config)
    minimum = int(config.thresholds.min_walk_forward_windows.value)

    rolls = [
        Roll(
            window=window,
            train=compute_metrics(
                dataset.within(window.train_start, window.train_end).frame, config
            ),
            test=compute_metrics(dataset.within(window.test_start, window.test_end).frame, config),
        )
        for window in windows
    ]

    reportable = [roll for roll in rolls if roll.reportable]
    if len(reportable) < minimum:
        return WalkForwardAnalysis(
            rolls=rolls,
            reportable_rolls=len(reportable),
            stable=None,
            consistent_direction=None,
            expectancy_range=None,
            mean_test_expectancy=None,
            undetermined_reason=(
                f"{len(windows)} window(s) fit in the period and {len(reportable)} "
                f"produced a reportable test result; {minimum} are needed. Either the "
                "period is shorter than one train+test window, or the test windows "
                "hold too few setups to clear the sample floor."
            ),
        )

    expectancies = [float(roll.test.expectancy) for roll in reportable]
    low, high = min(expectancies), max(expectancies)
    consistent = all(value > 0 for value in expectancies) or all(
        value < 0 for value in expectancies
    )

    return WalkForwardAnalysis(
        rolls=rolls,
        reportable_rolls=len(reportable),
        # Stable means every roll pointed the same way *and* that way was
        # up. A model that consistently loses is consistent, not stable.
        stable=consistent and low > 0,
        consistent_direction=consistent,
        expectancy_range=(low, high),
        mean_test_expectancy=sum(expectancies) / len(expectancies),
    )
