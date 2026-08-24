"""Score monotonicity: does a higher ARGUS score actually mean a better outcome?

## Why this is the load-bearing test, not a nice-to-have

Two questions hide inside "does ARGUS work", and they have different
answers. The first is whether the *pattern* has any edge. The second is
whether the *score* orders it. A system can pass the first and fail the
second — every setup performs identically well, and the 92 tells you
nothing the 41 didn't — and in that case the score is decoration. Worse,
it is decoration that gets acted on, because Module 14 gates qualification
on it and the whole live scanner will rank by it.

So: bucket setups by `argus_score`, compute the metric suite per bucket,
and ask whether outcomes improve as the score rises. If a 90-score setup
performs the same as a 40-score one, the scoring system has failed
regardless of anything else the evaluation says.

## What "improve systematically" means, precisely

Three separate readings, because collapsing them into one verdict would
hide which part broke:

- **`spearman`** — rank correlation between bucket order and the bucket's
  expectancy. The overall shape, in one number.
- **`violations`** — consecutive bucket pairs where the higher bucket
  underperformed the lower one by more than `monotonicity_tolerance`.
  Where the shape breaks, named.
- **`top_vs_bottom`** — the highest reportable bucket's expectancy minus
  the lowest's. The question a user actually asks: is a high score worth
  more than a low one?

`monotonic` is true only when there are no violations *and* the top beats
the bottom. A curve that rises and then collapses at the top has no
violations at its low end and is not monotonic, and this is where that
gets caught.

## Thin buckets report nothing

A bucket below `min_bucket_sample` gets a count, `INSUFFICIENT`, and no
expectancy — and is then excluded from the correlation and the pair
comparisons rather than contributing a noisy point. The top buckets are
always the thinnest (that is what a selective score means), so a
monotonicity analysis that let them speak from twelve setups would be
most confident exactly where it has least evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from core.historical_similarity.statistics import SampleSufficiency
from core.model_validation_evaluation.evaluation.config import EvaluationConfig
from core.model_validation_evaluation.evaluation.metrics import MetricSuite, compute_metrics


@dataclass(frozen=True, slots=True)
class ScoreBucket:
    """One score band's population and how it did."""

    label: str
    low: float
    high: float
    metrics: MetricSuite

    @property
    def reportable(self) -> bool:
        return self.metrics.reportable and self.metrics.expectancy is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "low": self.low,
            "high": self.high,
            "metrics": self.metrics.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class MonotonicityViolation:
    """A consecutive pair where a higher score did worse."""

    lower: str
    higher: str
    lower_expectancy: float
    higher_expectancy: float

    @property
    def shortfall(self) -> float:
        return self.lower_expectancy - self.higher_expectancy

    def as_dict(self) -> dict[str, Any]:
        return {
            "lower": self.lower,
            "higher": self.higher,
            "lower_expectancy": self.lower_expectancy,
            "higher_expectancy": self.higher_expectancy,
            "shortfall": self.shortfall,
        }


@dataclass(frozen=True, slots=True)
class MonotonicityAnalysis:
    """Whether the score orders outcomes, and where it does not."""

    buckets: list[ScoreBucket]
    reportable_buckets: int
    monotonic: bool | None
    spearman: float | None
    top_vs_bottom: float | None
    violations: list[MonotonicityViolation] = field(default_factory=list)
    #: Present when no verdict was possible, saying why.
    undetermined_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "buckets": [bucket.as_dict() for bucket in self.buckets],
            "reportable_buckets": self.reportable_buckets,
            "monotonic": self.monotonic,
            "spearman": self.spearman,
            "top_vs_bottom": self.top_vs_bottom,
            "violations": [violation.as_dict() for violation in self.violations],
            "undetermined_reason": self.undetermined_reason,
            "measure": "benchmark_relative_expectancy",
        }

    def summary(self) -> str:
        if self.monotonic is None:
            return f"Monotonicity undetermined: {self.undetermined_reason}"
        verdict = "holds" if self.monotonic else "does not hold"
        return (
            f"Score monotonicity {verdict} across {self.reportable_buckets} reportable "
            f"buckets (spearman={self.spearman:.3f}, top-bottom="
            f"{self.top_vs_bottom:+.4f}, {len(self.violations)} violation(s))."
        )


def analyse_monotonicity(
    frame: pd.DataFrame, config: EvaluationConfig | None = None
) -> MonotonicityAnalysis:
    """Bucket by score, measure each bucket, and judge the shape.

    Only qualified setups are bucketed — an unqualified setup has no
    score to bucket by. That is not a filter this analysis chooses; it is
    what "bucket by `argus_score`" means when the score is NULL.
    """
    config = config or EvaluationConfig()
    buckets = build_buckets(frame, config)
    reportable = [bucket for bucket in buckets if bucket.reportable]
    minimum = int(config.thresholds.min_buckets_for_monotonicity.value)

    if len(reportable) < minimum:
        return MonotonicityAnalysis(
            buckets=buckets,
            reportable_buckets=len(reportable),
            monotonic=None,
            spearman=None,
            top_vs_bottom=None,
            undetermined_reason=(
                f"{len(reportable)} bucket(s) cleared the {int(config.thresholds.min_bucket_sample.value)}-setup "
                f"floor; {minimum} are needed before a trend can be observed at all. "
                "This is the expected answer until a real historical scan has run."
            ),
        )

    expectancies = [bucket.metrics.expectancy for bucket in reportable]
    tolerance = config.thresholds.monotonicity_tolerance.value

    violations = [
        MonotonicityViolation(
            lower=lower.label,
            higher=higher.label,
            lower_expectancy=float(lower.metrics.expectancy),
            higher_expectancy=float(higher.metrics.expectancy),
        )
        for lower, higher in zip(reportable, reportable[1:], strict=False)
        if float(lower.metrics.expectancy) - float(higher.metrics.expectancy) > tolerance
    ]

    top_vs_bottom = float(expectancies[-1]) - float(expectancies[0])
    spearman = _spearman(np.arange(len(expectancies), dtype=float), np.array(expectancies))

    return MonotonicityAnalysis(
        buckets=buckets,
        reportable_buckets=len(reportable),
        monotonic=not violations and top_vs_bottom > 0,
        spearman=spearman,
        top_vs_bottom=top_vs_bottom,
        violations=violations,
    )


def build_buckets(frame: pd.DataFrame, config: EvaluationConfig | None = None) -> list[ScoreBucket]:
    """One `ScoreBucket` per configured band, thin ones included.

    Thin buckets are kept in the list rather than dropped: "the 90-100
    bucket holds four setups" is a finding about the score's selectivity,
    and a list that silently omitted it would read as though nothing ever
    scored above 90.
    """
    config = config or EvaluationConfig()
    edges = config.buckets.edges
    labels = config.buckets.labels()

    if frame.empty:
        return [
            ScoreBucket(
                label=label,
                low=low,
                high=high,
                metrics=compute_metrics(frame, config),
            )
            for label, low, high in zip(labels, edges, edges[1:], strict=False)
        ]

    scored = frame[frame["argus_score"].notna()]
    scores = pd.to_numeric(scored["argus_score"], errors="coerce")

    buckets: list[ScoreBucket] = []
    for index, (label, low, high) in enumerate(zip(labels, edges, edges[1:], strict=False)):
        # Half-open [low, high) everywhere except the top band, which
        # includes 100 — otherwise a perfect score would fall outside
        # every bucket, which is the one score most worth seeing.
        last = index == len(labels) - 1
        mask = (scores >= low) & ((scores <= high) if last else (scores < high))
        buckets.append(
            ScoreBucket(
                label=label,
                low=low,
                high=high,
                metrics=compute_metrics(scored[mask], config),
            )
        )
    return buckets


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    """One score band's predicted-vs-observed pair."""

    low: float
    high: float
    count: int
    mean_score: float | None
    observed_success_rate: float | None
    sufficiency: SampleSufficiency

    def as_dict(self) -> dict[str, Any]:
        return {
            "low": self.low,
            "high": self.high,
            "count": self.count,
            "mean_score": self.mean_score,
            "observed_success_rate": self.observed_success_rate,
            "sufficiency": self.sufficiency.value,
        }


@dataclass(frozen=True, slots=True)
class CalibrationCurve:
    """Observed success rate against score, bin by bin.

    Read the `interpretation` field before the numbers. ARGUS publishes no
    probability — Module 13 stores `probability` as NULL, deliberately,
    with its definition recorded alongside so the absence is unambiguous —
    so this is **not** a check that a stated probability was well
    calibrated. There is no stated probability to check.

    What it is: the empirical curve a future probability model would be
    fitted against, and the evidence for whether `argus_score` carries
    anything monotone in success rate at all. Calling it "calibration"
    without that caveat would be the exact overstatement Module 13 refused
    to make.
    """

    bins: list[CalibrationBin]
    interpretation: str = (
        "ARGUS states no probability (Module 13 stores probability as NULL). These "
        "are observed success rates by score band — the curve a probability model "
        "would later be fitted to, not a check of one that exists."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "bins": [entry.as_dict() for entry in self.bins],
            "interpretation": self.interpretation,
        }


def calibration_curve(
    frame: pd.DataFrame, config: EvaluationConfig | None = None
) -> CalibrationCurve:
    """Observed success rate per equal-width score bin."""
    from core.model_validation_evaluation.evaluation.metrics import RESOLVED_STATUSES, SUCCESS

    config = config or EvaluationConfig()
    count = int(config.thresholds.calibration_bins.value)
    floor = config.thresholds.min_calibration_bin_sample.value
    low_end, high_end = config.buckets.score_range
    edges = np.linspace(low_end, high_end, count + 1)

    if frame.empty:
        return CalibrationCurve(
            bins=[
                CalibrationBin(
                    low=float(low),
                    high=float(high),
                    count=0,
                    mean_score=None,
                    observed_success_rate=None,
                    sufficiency=SampleSufficiency.INSUFFICIENT,
                )
                for low, high in zip(edges, edges[1:], strict=False)
            ]
        )

    resolved = frame[frame["argus_score"].notna() & frame["outcome_status"].isin(RESOLVED_STATUSES)]
    scores = pd.to_numeric(resolved["argus_score"], errors="coerce")

    bins: list[CalibrationBin] = []
    for index, (low, high) in enumerate(zip(edges, edges[1:], strict=False)):
        last = index == count - 1
        mask = (scores >= low) & ((scores <= high) if last else (scores < high))
        subset = resolved[mask]
        sufficient = len(subset) >= floor
        bins.append(
            CalibrationBin(
                low=float(low),
                high=float(high),
                count=len(subset),
                mean_score=float(scores[mask].mean()) if len(subset) else None,
                observed_success_rate=(
                    float((subset["outcome_status"] == SUCCESS).mean()) if sufficient else None
                ),
                sufficiency=(
                    SampleSufficiency.ADEQUATE if sufficient else SampleSufficiency.INSUFFICIENT
                ),
            )
        )
    return CalibrationCurve(bins=bins)


def _spearman(ranks: np.ndarray, values: np.ndarray) -> float | None:
    """Rank correlation, without pulling in scipy for one function.

    `ranks` is already 0..n-1 by construction (buckets are ordered), so
    only `values` needs ranking. Ties get average ranks, which is what
    matters for the degenerate case this analysis is looking for — every
    bucket performing identically.
    """
    if len(values) < 2:
        return None
    value_ranks = pd.Series(values).rank().to_numpy(dtype=float)
    if np.std(ranks) == 0 or np.std(value_ranks) == 0:
        # Every bucket identical. Not an error and not a correlation:
        # exactly the "the score tells you nothing" case.
        return 0.0
    return float(np.corrcoef(ranks, value_ranks)[0, 1])
