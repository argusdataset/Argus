"""Outcome statistics, and refusing to compute them when N is too small.

## The failure this file exists to prevent

Two historical analogues, one of which returned +40% and one −10%. The
median is +15%. It is arithmetically correct, it renders beautifully, and
it is worthless — the interval around it spans essentially every outcome a
stock can have. Presented next to a statistic computed from 200 cases it
is indistinguishable, and a reader has no way to tell which is which.

So sufficiency is a **result state**, not a caveat in a footnote:

| State | Meaning | Statistics |
|---|---|---|
| `INSUFFICIENT` | Below the floor | **None reported at all** |
| `SPARSE` | Above the floor, below preferred | Reported, flagged, with intervals |
| `ADEQUATE` | Above preferred | Reported, flagged, with intervals |

`INSUFFICIENT` returns `None` for every statistic rather than a number
nobody should use. That is deliberate friction: a downstream module must
handle the absence explicitly, and cannot accidentally read a
two-sample median as evidence.

This is the same discipline Module 08 applied to features (`None`, never
a filled `0.0`) and Module 09 to eligibility (`INSUFFICIENT_EVIDENCE`,
never a low score).

## Intervals accompany every reported statistic

Even in the `ADEQUATE` band. A failure rate of 0.4 from 30 cases has a
95% interval of roughly ±0.18 — wide enough that it should never be
quoted bare. The interval is computed and carried so a consumer has to
look at it.

Proportions use the Wilson score interval rather than the normal
approximation, which is badly behaved for small N and for rates near 0 or
1 — precisely the region a failure rate lives in.

## Not a score

Everything here is descriptive. `failure_rate` is a count of what
happened, not a prediction and not a component of `argus_score`. Module 13
decides what, if anything, to do with it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd

from core.historical_similarity.config import SimilarityThresholds
from infra.db.enums import OutcomeStatus

#: Outcome statuses that count as the setup having failed.
#:
#: `EXPIRED` is deliberately NOT here. A setup that ran out of window
#: without resolving did not fail — it did not conclude, and folding it
#: into a failure rate would overstate failure by counting indecision as
#: a loss. `INVALIDATED` and `NO_VALID_OUTCOME` are excluded from the
#: denominator entirely for the same reason.
FAILURE_STATUSES: frozenset[OutcomeStatus] = frozenset({OutcomeStatus.FAILED})

#: Statuses that are resolved enough to contribute to a failure rate.
RESOLVED_STATUSES: frozenset[OutcomeStatus] = frozenset(
    {OutcomeStatus.SUCCESS, OutcomeStatus.FAILED}
)

#: Percentiles reported for every distribution.
REPORTED_PERCENTILES: tuple[int, ...] = (10, 25, 50, 75, 90)

#: Standard normal quantile for a 95% interval. A definitional constant.
Z_95 = 1.959963984540054


class SampleSufficiency(StrEnum):
    """Whether the analogue count supports reporting statistics at all."""

    INSUFFICIENT = "INSUFFICIENT"
    SPARSE = "SPARSE"
    ADEQUATE = "ADEQUATE"


@dataclass(frozen=True, slots=True)
class Interval:
    """A 95% interval, carried alongside every point estimate."""

    low: float
    high: float

    @property
    def width(self) -> float:
        return self.high - self.low

    def as_dict(self) -> dict[str, float]:
        return {"low": self.low, "high": self.high, "width": self.width}


@dataclass(frozen=True, slots=True)
class Distribution:
    """Percentiles of one outcome measure, with its sample size."""

    count: int
    percentiles: dict[int, float]
    mean: float
    #: Interval around the mean. Wide intervals are the honest signal that
    #: a small sample cannot support the precision the percentiles imply.
    mean_interval: Interval

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "percentiles": {str(k): v for k, v in self.percentiles.items()},
            "mean": self.mean,
            "mean_interval": self.mean_interval.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class OutcomeStatistics:
    """What happened to a set of analogues.

    Every statistic is `None` when `sufficiency is INSUFFICIENT`. There is
    no partial mode where the count is trusted but the median is not — the
    whole set of numbers stands or falls together.
    """

    sample_count: int
    sufficiency: SampleSufficiency
    median_outcome: float | None = None
    average_outcome: float | None = None
    failure_rate: float | None = None
    failure_rate_interval: Interval | None = None
    #: How many cases were resolved enough to contribute a failure rate.
    resolved_count: int = 0
    mfe: Distribution | None = None
    mae: Distribution | None = None
    expansion_magnitude: Distribution | None = None
    time_to_expansion: Distribution | None = None
    outcome_by_regime: dict[str, dict[str, Any]] | None = None

    @property
    def reportable(self) -> bool:
        return self.sufficiency is not SampleSufficiency.INSUFFICIENT


def assess_sufficiency(count: int, thresholds: SimilarityThresholds) -> SampleSufficiency:
    if count < thresholds.min_samples_for_statistics.value:
        return SampleSufficiency.INSUFFICIENT
    if count < thresholds.preferred_samples.value:
        return SampleSufficiency.SPARSE
    return SampleSufficiency.ADEQUATE


def summarize(
    outcomes: pd.DataFrame, thresholds: SimilarityThresholds | None = None
) -> OutcomeStatistics:
    """Descriptive statistics over a set of analogue outcomes.

    Returns a bare count and `INSUFFICIENT` when the sample is too small,
    with every statistic `None`.
    """
    thresholds = thresholds or SimilarityThresholds()
    count = len(outcomes)
    sufficiency = assess_sufficiency(count, thresholds)

    if sufficiency is SampleSufficiency.INSUFFICIENT:
        return OutcomeStatistics(sample_count=count, sufficiency=sufficiency)

    returns = _numeric(outcomes, "realized_return")
    failure_rate, failure_interval, resolved = _failure_rate(outcomes)

    return OutcomeStatistics(
        sample_count=count,
        sufficiency=sufficiency,
        median_outcome=float(returns.median()) if not returns.empty else None,
        average_outcome=float(returns.mean()) if not returns.empty else None,
        failure_rate=failure_rate,
        failure_rate_interval=failure_interval,
        resolved_count=resolved,
        mfe=_distribution(_numeric(outcomes, "mfe")),
        mae=_distribution(_numeric(outcomes, "mae")),
        # Expansion magnitude is MFE restricted to setups that actually
        # succeeded: "how far did it run when it ran", which is a
        # different question from "how far did it run on average".
        expansion_magnitude=_distribution(
            _numeric(outcomes[_status_mask(outcomes, {OutcomeStatus.SUCCESS})], "mfe")
        ),
        time_to_expansion=_distribution(
            _durations(outcomes[_status_mask(outcomes, {OutcomeStatus.SUCCESS})], "time_to_mfe")
        ),
        outcome_by_regime=_by_regime(outcomes, thresholds),
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if frame.empty or column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").dropna()


def _durations(frame: pd.DataFrame, column: str) -> pd.Series:
    """A timedelta column as days, for percentile reporting."""
    if frame.empty or column not in frame.columns:
        return pd.Series(dtype=float)
    values = frame[column].dropna()
    if values.empty:
        return pd.Series(dtype=float)
    return values.map(
        lambda v: v.total_seconds() / 86400.0 if isinstance(v, timedelta) else float("nan")
    ).dropna()


def _status_mask(frame: pd.DataFrame, statuses: set[OutcomeStatus]) -> pd.Series:
    if frame.empty or "outcome_status" not in frame.columns:
        return pd.Series(dtype=bool)
    wanted = {s.value for s in statuses}
    return frame["outcome_status"].astype(str).isin(wanted)


def _distribution(values: pd.Series) -> Distribution | None:
    """Percentiles plus a mean interval, or None when there is nothing to describe."""
    if values.empty:
        return None
    array = values.to_numpy(dtype=float)
    mean = float(array.mean())
    return Distribution(
        count=len(array),
        percentiles={p: float(np.percentile(array, p)) for p in REPORTED_PERCENTILES},
        mean=mean,
        mean_interval=_mean_interval(array),
    )


def _mean_interval(array: np.ndarray) -> Interval:
    """95% interval for a mean. Degenerate for n=1, and honestly so."""
    n = len(array)
    if n < 2:
        return Interval(low=float(array[0]), high=float(array[0]))
    standard_error = float(array.std(ddof=1)) / math.sqrt(n)
    mean = float(array.mean())
    return Interval(low=mean - Z_95 * standard_error, high=mean + Z_95 * standard_error)


def _failure_rate(outcomes: pd.DataFrame) -> tuple[float | None, Interval | None, int]:
    """Failures over *resolved* cases, with a Wilson interval.

    Only SUCCESS and FAILED enter the denominator. An EXPIRED setup did
    not fail — it did not conclude — and counting indecision as a loss
    would systematically overstate failure.
    """
    resolved = outcomes[_status_mask(outcomes, set(RESOLVED_STATUSES))]
    total = len(resolved)
    if total == 0:
        return None, None, 0

    failures = int(_status_mask(resolved, set(FAILURE_STATUSES)).sum())
    rate = failures / total
    return rate, _wilson_interval(failures, total), total


def _wilson_interval(successes: int, total: int) -> Interval:
    """Wilson score interval for a proportion.

    Chosen over the normal approximation because the latter misbehaves
    badly at small N and near 0 or 1 — it happily produces intervals that
    extend below zero, which for a failure rate is nonsense.
    """
    if total == 0:
        return Interval(low=0.0, high=1.0)
    proportion = successes / total
    denominator = 1 + Z_95**2 / total
    centre = (proportion + Z_95**2 / (2 * total)) / denominator
    margin = (
        Z_95
        * math.sqrt(proportion * (1 - proportion) / total + Z_95**2 / (4 * total**2))
        / denominator
    )
    return Interval(low=max(0.0, centre - margin), high=min(1.0, centre + margin))


def _by_regime(
    outcomes: pd.DataFrame, thresholds: SimilarityThresholds
) -> dict[str, dict[str, Any]] | None:
    """Outcomes split by the market regime they concluded in.

    Each bucket carries its own sufficiency. Splitting a small sample
    makes every bucket smaller, so a set that is SPARSE overall is often
    INSUFFICIENT once split — and reporting a per-regime median from two
    cases would be exactly the error this module refuses to make at the
    top level.
    """
    if outcomes.empty or "market_regime_at_outcome" not in outcomes.columns:
        return None

    buckets: dict[str, dict[str, Any]] = {}
    for regime, group in outcomes.groupby(
        outcomes["market_regime_at_outcome"].astype(str), dropna=True
    ):
        returns = _numeric(group, "realized_return")
        sufficiency = assess_sufficiency(len(group), thresholds)
        buckets[str(regime)] = {
            "count": len(group),
            "sufficiency": sufficiency.value,
            "median_outcome": (
                float(returns.median())
                if sufficiency is not SampleSufficiency.INSUFFICIENT and not returns.empty
                else None
            ),
        }
    return buckets or None
