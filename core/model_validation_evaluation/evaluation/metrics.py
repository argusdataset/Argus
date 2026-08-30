"""The standard metric suite: does this work, and under what conditions.

## The two definitions everything else rests on

**Predicted positive** = the setup qualified. ARGUS committed to tracking
it because its `argus_score` and `confidence` cleared Module 14's bar.
A detected-but-unqualified setup is a predicted *negative*: ARGUS saw the
base and declined.

**Actual positive** = `outcome_status is SUCCESS`, as Module 15 recorded
it under a named snapshot. Not recomputed here — see `config.py` on why
re-deciding the label in this module would be the most damaging possible
shortcut.

From those two, the confusion matrix is ordinary and so are precision,
recall and false-positive rate. What is *not* ordinary is that both
classes are actually observable: ARGUS records an outcome for every
concluded setup, qualified or not, so the false negatives are real
measurements rather than the usual unknowable quantity. That is a direct
consequence of Module 14's decision that detection does not require a
score.

## Hit rate is not precision

Precision asks how often a committed setup met Module 15's success
definition (+1.5×ATR / -0.75×ATR / 60 days, a placeholder). Hit rate asks how often
it beat its benchmark. They answer different questions and can move in
opposite directions — a setup that rose 6% while SPY fell 4% is not a
success and is a hit — so they are reported separately and never averaged.

## Benchmark-relative throughout

Every return statistic here reads `benchmark_relative_return`, never
`realized_return`. A 30% gain in a year the market gained 35% is not
evidence of anything, and a metric suite that reported it as one would
flatter the model exactly in the periods where it mattered least. Absolute
return is available on the row for anyone who wants it; nothing in this
module's headline numbers uses it.

## Drawdown is a path property

Reported as the deepest peak-to-trough decline of the cumulative
benchmark-relative return, with setups ordered by detection date — not as
the worst single MAE, which is a property of one setup rather than of the
strategy. Both are here; only the first is called drawdown.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from core.historical_similarity.statistics import (
    Interval,
    SampleSufficiency,
    mean_confidence_interval,
    wilson_interval,
)
from core.model_validation_evaluation.evaluation.config import (
    EvaluationConfig,
    EvaluationThresholds,
)
from infra.db.enums import OutcomeStatus

#: Statuses that resolved one way or the other. EXPIRED, INVALIDATED and
#: NO_VALID_OUTCOME are deliberately absent: Module 15 established that
#: they are first-class results, not failures in disguise, and counting
#: indecision as a loss would systematically overstate failure.
RESOLVED_STATUSES: frozenset[str] = frozenset(
    {OutcomeStatus.SUCCESS.value, OutcomeStatus.FAILED.value}
)
SUCCESS = OutcomeStatus.SUCCESS.value


@dataclass(frozen=True, slots=True)
class ConfusionMatrix:
    """Committed-or-not against succeeded-or-not, over resolved setups."""

    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int

    @property
    def total(self) -> int:
        return self.true_positive + self.false_positive + self.true_negative + self.false_negative

    def as_dict(self) -> dict[str, int]:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "true_negative": self.true_negative,
            "false_negative": self.false_negative,
            "total": self.total,
        }


@dataclass(frozen=True, slots=True)
class MetricSuite:
    """Everything the standard suite answers, or honestly declines to.

    Every rate is `None` rather than 0.0 when its denominator is empty.
    Module 08 established the rule and it matters most here: a precision
    of 0.0 says every committed setup failed, and an empty denominator
    says ARGUS never committed to anything. Reporting the second as the
    first would be the most misleading number this module could produce.
    """

    sample_size: int
    sufficiency: SampleSufficiency
    confusion: ConfusionMatrix
    precision: float | None = None
    precision_interval: Interval | None = None
    recall: float | None = None
    recall_interval: Interval | None = None
    hit_rate: float | None = None
    hit_rate_interval: Interval | None = None
    false_positive_rate: float | None = None
    expectancy: float | None = None
    expectancy_interval: Interval | None = None
    median_relative_return: float | None = None
    mean_mfe: float | None = None
    mean_mae: float | None = None
    median_mfe: float | None = None
    median_mae: float | None = None
    mfe_mae_ratio: float | None = None
    max_drawdown: float | None = None
    worst_single_mae: float | None = None
    #: Counts by `outcome_status`, so the unresolved statuses are visible
    #: rather than silently excluded from every rate above.
    status_counts: dict[str, int] = field(default_factory=dict)

    @property
    def reportable(self) -> bool:
        return self.sufficiency is not SampleSufficiency.INSUFFICIENT

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "sufficiency": self.sufficiency.value,
            "confusion": self.confusion.as_dict(),
            "precision": self.precision,
            "precision_interval": _interval(self.precision_interval),
            "recall": self.recall,
            "recall_interval": _interval(self.recall_interval),
            "hit_rate": self.hit_rate,
            "hit_rate_interval": _interval(self.hit_rate_interval),
            "false_positive_rate": self.false_positive_rate,
            "expectancy": self.expectancy,
            "expectancy_interval": _interval(self.expectancy_interval),
            "median_relative_return": self.median_relative_return,
            "mean_mfe": self.mean_mfe,
            "mean_mae": self.mean_mae,
            "median_mfe": self.median_mfe,
            "median_mae": self.median_mae,
            "mfe_mae_ratio": self.mfe_mae_ratio,
            "max_drawdown": self.max_drawdown,
            "worst_single_mae": self.worst_single_mae,
            "status_counts": self.status_counts,
            "return_basis": "benchmark_relative",
        }


def compute_metrics(frame: pd.DataFrame, config: EvaluationConfig | None = None) -> MetricSuite:
    """The full suite over one set of evaluated setups.

    `frame` is a slice of `EvaluationDataset.frame` — the whole thing, a
    score bucket, a regime, a walk-forward test window. Every caller in
    this module passes a slice of one loaded frame rather than re-querying.
    """
    config = config or EvaluationConfig()
    thresholds = config.thresholds

    if frame.empty:
        return MetricSuite(
            sample_size=0,
            sufficiency=SampleSufficiency.INSUFFICIENT,
            confusion=ConfusionMatrix(0, 0, 0, 0),
        )

    confusion = confusion_matrix(frame)
    sufficiency = assess(len(frame), thresholds)
    status_counts = _status_counts(frame)

    if sufficiency is SampleSufficiency.INSUFFICIENT:
        # A count and the raw matrix, and nothing that reads as a finding.
        return MetricSuite(
            sample_size=len(frame),
            sufficiency=sufficiency,
            confusion=confusion,
            status_counts=status_counts,
        )

    qualified = frame[frame["qualifying_signal_id"].notna()]
    relative = _numeric(qualified, "benchmark_relative_return")

    return MetricSuite(
        sample_size=len(frame),
        sufficiency=sufficiency,
        confusion=confusion,
        precision=_rate(
            confusion.true_positive, confusion.true_positive + confusion.false_positive
        ),
        precision_interval=_proportion_interval(
            confusion.true_positive, confusion.true_positive + confusion.false_positive
        ),
        recall=_rate(confusion.true_positive, confusion.true_positive + confusion.false_negative),
        recall_interval=_proportion_interval(
            confusion.true_positive, confusion.true_positive + confusion.false_negative
        ),
        hit_rate=_hit_rate(relative),
        hit_rate_interval=_hit_rate_interval(relative),
        false_positive_rate=_rate(
            confusion.false_positive, confusion.false_positive + confusion.true_negative
        ),
        expectancy=_mean(relative),
        expectancy_interval=(mean_confidence_interval(relative) if len(relative) else None),
        median_relative_return=_median(relative),
        mean_mfe=_mean(_numeric(qualified, "mfe")),
        mean_mae=_mean(_numeric(qualified, "mae")),
        median_mfe=_median(_numeric(qualified, "mfe")),
        median_mae=_median(_numeric(qualified, "mae")),
        mfe_mae_ratio=_mfe_mae_ratio(qualified),
        max_drawdown=max_drawdown(qualified),
        worst_single_mae=_worst_mae(qualified),
        status_counts=status_counts,
    )


def confusion_matrix(frame: pd.DataFrame) -> ConfusionMatrix:
    """Committed vs succeeded, over the setups that actually resolved.

    Unresolved setups (EXPIRED, INVALIDATED, NO_VALID_OUTCOME) are
    excluded from the matrix entirely rather than assigned to a class.
    They are visible in `status_counts`, which is where a reader can see
    how much of the population never answered the question.
    """
    resolved = frame[frame["outcome_status"].isin(RESOLVED_STATUSES)]
    if resolved.empty:
        return ConfusionMatrix(0, 0, 0, 0)

    committed = resolved["qualifying_signal_id"].notna()
    succeeded = resolved["outcome_status"] == SUCCESS

    return ConfusionMatrix(
        true_positive=int((committed & succeeded).sum()),
        false_positive=int((committed & ~succeeded).sum()),
        true_negative=int((~committed & ~succeeded).sum()),
        false_negative=int((~committed & succeeded).sum()),
    )


def max_drawdown(frame: pd.DataFrame) -> float | None:
    """Deepest peak-to-trough decline of cumulative relative return.

    Setups ordered by detection date, returns summed rather than
    compounded: ARGUS records no position sizing, so compounding would
    invent an allocation policy and report its consequences as a property
    of the model.

    Returns a non-positive number, or 0.0 for a curve that only rises —
    which is a measurement ("no drawdown observed"), not an absence.
    """
    if frame.empty:
        return None
    ordered = frame.sort_values("detected_at")
    relative = _numeric(ordered, "benchmark_relative_return")
    if not len(relative):
        return None
    curve = np.cumsum(relative)
    peak = np.maximum.accumulate(curve)
    return float((curve - peak).min())


def assess(count: int, thresholds: EvaluationThresholds) -> SampleSufficiency:
    """Module 11's three-way sufficiency, against this module's floors.

    SPARSE has no separate floor here: a bucket either clears
    `min_bucket_sample` or it does not. The distinction Module 11 draws
    between SPARSE and ADEQUATE is about how far a similarity claim can
    be pushed; here the question is binary — report, or report a count.
    """
    if count < thresholds.min_bucket_sample.value:
        return SampleSufficiency.INSUFFICIENT
    return SampleSufficiency.ADEQUATE


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _numeric(frame: pd.DataFrame, column: str) -> np.ndarray:
    if frame.empty or column not in frame:
        return np.array([], dtype=float)
    series = pd.to_numeric(frame[column], errors="coerce").dropna()
    return series.to_numpy(dtype=float)


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _proportion_interval(successes: int, total: int) -> Interval | None:
    return wilson_interval(successes, total) if total else None


def _hit_rate(relative: np.ndarray) -> float | None:
    if not len(relative):
        return None
    return float((relative > 0).sum()) / len(relative)


def _hit_rate_interval(relative: np.ndarray) -> Interval | None:
    if not len(relative):
        return None
    return wilson_interval(int((relative > 0).sum()), len(relative))


def _mean(values: np.ndarray) -> float | None:
    return float(values.mean()) if len(values) else None


def _median(values: np.ndarray) -> float | None:
    return float(np.median(values)) if len(values) else None


def _mfe_mae_ratio(frame: pd.DataFrame) -> float | None:
    """Mean MFE over mean absolute MAE. None when either is unmeasured.

    Not a per-setup ratio averaged afterwards: a setup with a near-zero
    MAE produces an enormous ratio, and the mean of those is dominated by
    whichever setup happened to have the calmest entry.
    """
    mfe = _mean(_numeric(frame, "mfe"))
    mae = _mean(np.abs(_numeric(frame, "mae")))
    if mfe is None or mae is None or math.isclose(mae, 0.0):
        return None
    return mfe / mae


def _worst_mae(frame: pd.DataFrame) -> float | None:
    values = _numeric(frame, "mae")
    return float(values.min()) if len(values) else None


def _status_counts(frame: pd.DataFrame) -> dict[str, int]:
    counts = frame["outcome_status"].value_counts()
    return {str(status): int(count) for status, count in counts.items()}


def _interval(interval: Interval | None) -> dict[str, float] | None:
    return interval.as_dict() if interval else None
