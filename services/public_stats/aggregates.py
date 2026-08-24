"""The four charts, computed from a dataset. Pure — no connection, no gate.

## Why this file cannot reach the database

The gate is enforced by shape (`gate.py`), and part of that shape is that
everything downstream of it takes an already-loaded dataset. A function
here that could open a connection could load whatever it liked, and the
structural test that keeps unapproved results out would become a test of
intent rather than of structure.

So every function here is `frame in, payload out`. A structural test
asserts no module in this service except `gate.py` imports a loader or
names the outcome tables.

## Four charts, not one number

Stated as a hard requirement early in the project: results are shown
across several distinct chart types, never a single aggregate. One number
is the shape a marketing page takes; four charts is the shape a claim
takes when it expects to be checked.

- **Win rate** — the full outcome distribution, including EXPIRED and
  INVALIDATED, not just success over failure.
- **Cumulative performance** — the path, so a good average built from one
  lucky stretch is visible as one lucky stretch.
- **Regime breakdown** — where it worked and where it did not.
- **MFE/MAE distribution** — the shape of the excursions, not their mean.

## Numbers come from Module 17, not from here

`compute_metrics` and `by_regime` are Module 17's. This module reshapes
their output for a chart and computes nothing statistical of its own —
because a public figure that disagreed with the evaluation report behind
it would be a worse problem than either number being wrong.

The one exception is histogram binning, which is presentation: how to
group excursions for reading, not what any excursion was.

## Sufficiency is a first-class output, not a filter

A bucket below the public floor is **kept** and marked, not dropped. A
chart that silently omitted its thin buckets would show a clean line and
imply completeness; one that shows a bar labelled "9 outcomes — too few
to report a rate" is telling the truth about what ARGUS knows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from core.historical_similarity.statistics import SampleSufficiency
from core.model_validation_evaluation.evaluation.breakdowns import by_regime
from core.model_validation_evaluation.evaluation.config import (
    EvaluationConfig,
    EvaluationThreshold,
    EvaluationThresholds,
)
from core.model_validation_evaluation.evaluation.metrics import MetricSuite, compute_metrics
from services.public_stats.config import PublicStatsConfig

__all__ = [
    "CHARTS",
    "CHART_CUMULATIVE",
    "CHART_EXCURSIONS",
    "CHART_REGIME",
    "CHART_WIN_RATE",
    "build_chart",
    "cumulative_performance",
    "excursion_distribution",
    "regime_breakdown",
    "win_rate_breakdown",
]

CHART_WIN_RATE = "win_rate"
CHART_CUMULATIVE = "cumulative_performance"
CHART_REGIME = "regime_breakdown"
CHART_EXCURSIONS = "excursion_distribution"

#: The closed set. A caller naming anything else gets a 404 rather than an
#: empty chart, because "no such chart" and "this chart is empty" are
#: different answers and only one is the caller's mistake.
CHARTS: tuple[str, ...] = (
    CHART_WIN_RATE,
    CHART_CUMULATIVE,
    CHART_REGIME,
    CHART_EXCURSIONS,
)

#: Prose attached to every insufficient bucket. Written for a reader with
#: no context, which is the whole constraint this module works under.
_THIN = (
    "Too few outcomes to report a rate honestly. The count is shown instead. "
    "ARGUS requires {floor} outcomes before publishing a percentage, because a "
    "rate computed from a handful of cases looks exactly like one computed from "
    "thousands."
)


@dataclass(frozen=True, slots=True)
class ChartPayload:
    """One chart's data, ready to plot, plus what it cannot say."""

    chart: str
    sample_size: int
    series: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    #: Human-readable, for a reader who is not going to open the docs.
    caption: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "chart": self.chart,
            "sample_size": self.sample_size,
            "series": self.series,
            "summary": self.summary,
            "caption": self.caption,
        }


def build_chart(
    chart: str, frame: pd.DataFrame, config: PublicStatsConfig | None = None
) -> ChartPayload:
    """Dispatch to one chart builder. Unknown names raise, never return empty."""
    builders = {
        CHART_WIN_RATE: win_rate_breakdown,
        CHART_CUMULATIVE: cumulative_performance,
        CHART_REGIME: regime_breakdown,
        CHART_EXCURSIONS: excursion_distribution,
    }
    if chart not in builders:
        raise KeyError(chart)
    return builders[chart](frame, config)


# --------------------------------------------------------------------------
# 1. Win rate
# --------------------------------------------------------------------------


def win_rate_breakdown(
    frame: pd.DataFrame, config: PublicStatsConfig | None = None
) -> ChartPayload:
    """Outcome distribution, every status, with rates only above the floor.

    EXPIRED and INVALIDATED are shown alongside SUCCESS and FAILED rather
    than folded away. Module 15 established that an expired setup did not
    fail — it did not conclude — and a public chart that quietly dropped
    the unresolved ones would report a success rate over a denominator
    ARGUS chose after seeing the results.
    """
    config = config or PublicStatsConfig()
    floor = int(config.settings.min_public_sample)
    suite = _suite(frame, config)

    counts = suite.status_counts
    total = sum(counts.values())
    series = [
        {
            "status": status,
            "count": count,
            "share": (count / total) if total else None,
        }
        for status, count in sorted(counts.items(), key=lambda item: -item[1])
    ]

    sufficient = total >= floor
    return ChartPayload(
        chart=CHART_WIN_RATE,
        sample_size=total,
        series=series,
        summary={
            "sufficiency": _sufficiency(total, floor).value,
            "resolved": suite.confusion.total,
            "precision": suite.precision if sufficient else None,
            "hit_rate": suite.hit_rate if sufficient else None,
            "unavailable": None if sufficient else _unavailable(total, floor),
        },
        caption=(
            f"Every concluded setup ARGUS has published, by outcome. {total} in total."
            if sufficient
            else f"{total} published outcome(s) — below the {floor} ARGUS requires "
            "before publishing a rate."
        ),
    )


# --------------------------------------------------------------------------
# 2. Cumulative performance
# --------------------------------------------------------------------------


def cumulative_performance(
    frame: pd.DataFrame, config: PublicStatsConfig | None = None
) -> ChartPayload:
    """Cumulative benchmark-relative return over time, oldest first.

    **Benchmark-relative, summed, not compounded.** Relative because a 30%
    gain in a year the market gained 35% is not evidence of anything, and
    Module 17 reports every return that way. Summed because ARGUS records
    no position sizing — compounding would invent an allocation policy and
    then present its consequences as a property of the model.

    Ordered by conclusion, not detection: the curve is what a reader would
    have watched happen, and a setup contributes on the day it resolved.
    """
    config = config or PublicStatsConfig()
    floor = int(config.settings.min_public_sample)
    minimum_points = int(config.settings.min_points_for_series)

    if frame.empty:
        return ChartPayload(
            chart=CHART_CUMULATIVE,
            sample_size=0,
            summary={"sufficiency": SampleSufficiency.INSUFFICIENT.value},
            caption="No published outcomes yet.",
        )

    qualified = frame[frame["qualifying_signal_id"].notna()].copy()
    qualified["_when"] = qualified["concluded_at"].fillna(qualified["detected_at"])
    qualified = qualified.dropna(subset=["_when", "benchmark_relative_return"])
    qualified = qualified.sort_values("_when")

    count = len(qualified)
    if count < minimum_points:
        return ChartPayload(
            chart=CHART_CUMULATIVE,
            sample_size=count,
            summary={
                "sufficiency": SampleSufficiency.INSUFFICIENT.value,
                "unavailable": _unavailable(count, floor),
            },
            caption=(
                f"{count} published outcome(s) with a measured return — a line needs at "
                f"least {minimum_points} points."
            ),
        )

    returns = pd.to_numeric(qualified["benchmark_relative_return"], errors="coerce").to_numpy(
        dtype=float
    )
    curve = np.cumsum(returns)
    stamps = [_iso(value) for value in qualified["_when"]]

    points = [
        {"date": stamp, "cumulative_relative_return": float(value), "outcome_index": index + 1}
        for index, (stamp, value) in enumerate(zip(stamps, curve, strict=True))
    ]
    points = _thin(points, int(config.settings.cumulative_max_points))

    peak = np.maximum.accumulate(curve)
    return ChartPayload(
        chart=CHART_CUMULATIVE,
        sample_size=count,
        series=points,
        summary={
            "sufficiency": _sufficiency(count, floor).value,
            "final_cumulative_relative_return": float(curve[-1]),
            "max_drawdown": float((curve - peak).min()),
            "basis": "benchmark_relative, summed (ARGUS records no position sizing)",
        },
        caption=(
            f"Cumulative return relative to benchmark across {count} published outcomes, "
            "in the order they concluded."
        ),
    )


# --------------------------------------------------------------------------
# 3. Regime breakdown
# --------------------------------------------------------------------------


def regime_breakdown(frame: pd.DataFrame, config: PublicStatsConfig | None = None) -> ChartPayload:
    """Performance by the market regime each outcome resolved in.

    Module 17's `by_regime`, reshaped. Every group is kept including the
    thin ones — a regime with nine outcomes is a fact about where ARGUS
    has no evidence, and dropping it would leave a chart implying the
    system has been tested everywhere it has not.
    """
    config = config or PublicStatsConfig()
    floor = int(config.settings.min_public_sample)
    breakdown = by_regime(frame, _evaluation_config(config))

    series: list[dict[str, Any]] = []
    for regime, suite in sorted(breakdown.groups.items()):
        sufficient = suite.sample_size >= floor
        series.append(
            {
                "regime": regime,
                "sample_size": suite.sample_size,
                "sufficiency": _sufficiency(suite.sample_size, floor).value,
                "hit_rate": suite.hit_rate if sufficient else None,
                "expectancy": suite.expectancy if sufficient else None,
                "unavailable": None if sufficient else _unavailable(suite.sample_size, floor),
            }
        )

    reportable = sum(1 for row in series if row["hit_rate"] is not None)
    return ChartPayload(
        chart=CHART_REGIME,
        sample_size=int(sum(row["sample_size"] for row in series)),
        series=series,
        summary={
            "regimes": len(series),
            "reportable_regimes": reportable,
            "floor": floor,
        },
        caption=(
            f"{reportable} of {len(series)} market regime(s) have enough published "
            "outcomes to report a rate."
            if series
            else "No published outcomes yet."
        ),
    )


# --------------------------------------------------------------------------
# 4. MFE / MAE distribution
# --------------------------------------------------------------------------


def excursion_distribution(
    frame: pd.DataFrame, config: PublicStatsConfig | None = None
) -> ChartPayload:
    """Histograms of maximum favourable and adverse excursion.

    Two distributions rather than two averages. A mean MFE of 18% is
    consistent with every setup running 18%, and with nine flat ones and a
    tenth that ran 180% — those are different systems and a reader
    deserves to see which one this is.

    Extremes are clipped into the outermost bins rather than dropped:
    every published outcome is counted, and the bin is labelled open-ended
    so the chart does not imply a boundary that is not there.
    """
    config = config or PublicStatsConfig()
    floor = int(config.settings.min_public_sample)

    mfe = _numeric(frame, "mfe")
    mae = _numeric(frame, "mae")
    count = max(len(mfe), len(mae))

    bins = int(config.settings.excursion_bins)
    clip = float(config.settings.excursion_clip)

    return ChartPayload(
        chart=CHART_EXCURSIONS,
        sample_size=count,
        series=[
            {"measure": "mfe", "bins": _histogram(mfe, bins=bins, low=0.0, high=clip)},
            {"measure": "mae", "bins": _histogram(mae, bins=bins, low=-clip, high=0.0)},
        ],
        summary={
            "sufficiency": _sufficiency(count, floor).value,
            "median_mfe": float(np.median(mfe)) if len(mfe) >= floor else None,
            "median_mae": float(np.median(mae)) if len(mae) >= floor else None,
            "unavailable": None if count >= floor else _unavailable(count, floor),
        },
        caption=(
            f"How far {count} published setups ran in ARGUS's favour and against it, "
            "before concluding."
            if count
            else "No published outcomes yet."
        ),
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _suite(frame: pd.DataFrame, config: PublicStatsConfig) -> MetricSuite:
    return compute_metrics(frame, _evaluation_config(config))


def _evaluation_config(config: PublicStatsConfig) -> EvaluationConfig:
    """Module 17's config, with this module's stricter public floor.

    Built here rather than held as a field so the two floors cannot drift:
    there is one public number, and Module 17's thresholds inherit it
    instead of carrying a second copy.
    """
    floor = float(config.settings.min_public_sample.value)
    return EvaluationConfig(
        thresholds=EvaluationThresholds(
            min_bucket_sample=EvaluationThreshold(
                value=floor,
                kind="calibratable",
                rationale="Module 20's public floor, applied to Module 17's analyses.",
            ),
            min_regime_sample=EvaluationThreshold(
                value=floor,
                kind="calibratable",
                rationale="Module 20's public floor, applied to Module 17's analyses.",
            ),
        )
    )


def _sufficiency(count: int, floor: int) -> SampleSufficiency:
    return SampleSufficiency.ADEQUATE if count >= floor else SampleSufficiency.INSUFFICIENT


def _unavailable(count: int, floor: int) -> dict[str, Any]:
    """Module 19's `Unavailable` shape, built as a plain dict.

    A dict rather than the Pydantic model because this payload is stored
    as JSONB and re-served verbatim; the model validates it on the way
    out in `schemas.py`.
    """
    return {
        "available": False,
        "reason": "insufficient_sample",
        "explanation": _THIN.format(floor=floor),
        "observed": count,
        "required": floor,
    }


def _numeric(frame: pd.DataFrame, column: str) -> np.ndarray:
    if frame.empty or column not in frame:
        return np.array([], dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").dropna().to_numpy(dtype=float)


def _histogram(values: np.ndarray, *, bins: int, low: float, high: float) -> list[dict[str, Any]]:
    """Counts per bin, with the outer bins open-ended.

    Returns the full set of bins even when empty, so a chart's x-axis is
    the same shape whether ARGUS has published four outcomes or forty
    thousand — an axis that changes with the data makes two charts
    impossible to compare by eye.
    """
    edges = np.linspace(low, high, bins + 1)
    if not len(values):
        return [_bin(edges[index], edges[index + 1], 0, index, bins) for index in range(bins)]

    clipped = np.clip(values, edges[0], edges[-1])
    counts, _ = np.histogram(clipped, bins=edges)
    return [
        _bin(edges[index], edges[index + 1], int(counts[index]), index, bins)
        for index in range(bins)
    ]


def _bin(low: float, high: float, count: int, index: int, total: int) -> dict[str, Any]:
    return {
        "low": float(low),
        "high": float(high),
        "count": count,
        # Open-ended edges are labelled, so the chart does not imply a
        # boundary that does not exist in the data.
        "open_low": index == 0,
        "open_high": index == total - 1,
    }


def _thin(points: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    """Every nth point, always keeping the first and last.

    The last point is the published cumulative figure, so it is exact
    regardless of thinning — a chart may lose intermediate detail, but the
    number a reader quotes must not be an artefact of downsampling.
    """
    if len(points) <= maximum or maximum < 2:
        return points
    step = len(points) / (maximum - 1)
    kept = [points[int(index * step)] for index in range(maximum - 1)]
    kept.append(points[-1])
    return kept


def _iso(value: Any) -> str:
    stamp = pd.Timestamp(value)
    return stamp.isoformat()
