"""The tri-state verdict, and the pure decision behind it.

## Three states, not two

`raised=True`, `raised=False`, and `raised=None` — the same discipline
Module 12's `RiskFlag` established and the reason is identical: "not
enough history to say" is not "normal volume", and collapsing the two
would make an undetermined reading look like a measured negative. So
`unavailable` is set exactly when `raised is None`, matching `RiskFlag`
field for field.

## `evaluate` takes counts, not a connection

Every number this function needs — today's count, the baseline total, how
far back the earliest known article reaches — is computed once by a
caller that owns the database round trip (`assessment.py` for one
security, `batch.py` for many in a single aggregate query). Keeping the
decision itself connection-free is what makes it testable with plain
integers and what makes `core/ingestion/tiers.py::decide` a fair model to
follow: the three-state logic is exactly as easy to get wrong as tier
due-ness was, and deserves the same kind of test that needs no database
at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

from core.data_validation.result import MissReason
from core.news_signals.config import NewsSignalThresholds

__all__ = ["NewsVolumeSignal", "evaluate"]


@dataclass(frozen=True, slots=True)
class NewsVolumeSignal:
    """One security's reading for one day."""

    security_id: UUID
    scan_date: date
    as_of: datetime
    #: None means undetermined. Never coerce it to False.
    raised: bool | None
    today_count: int
    #: None exactly when `raised` is None — there is no reliable baseline
    #: to report either.
    baseline_mean: float | None
    baseline_window_days: int
    multiple_threshold: float
    config_version_label: str
    #: Set exactly when `raised is None`.
    unavailable: MissReason | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def determined(self) -> bool:
        return self.raised is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "security_id": str(self.security_id),
            "scan_date": self.scan_date.isoformat(),
            "as_of": self.as_of.isoformat(),
            "raised": self.raised,
            "today_count": self.today_count,
            "baseline_mean": self.baseline_mean,
            "baseline_window_days": self.baseline_window_days,
            "multiple_threshold": self.multiple_threshold,
            "config_version_label": self.config_version_label,
            "unavailable": self.unavailable.value if self.unavailable else None,
            "detail": dict(self.detail),
        }


def evaluate(
    *,
    security_id: UUID,
    scan_date: date,
    as_of: datetime,
    today_count: int,
    baseline_total: int,
    earliest_known_event_date: date | None,
    thresholds: NewsSignalThresholds,
    config_version_label: str,
) -> NewsVolumeSignal:
    """The three-state decision, from counts a caller already fetched.

    ## Determinability

    A baseline is trustworthy only once the security has been observable
    for the whole window: `earliest_known_event_date` is the oldest
    article ARGUS could see for this security as of `as_of`, and if that
    date is not at least `baseline_window_days` before `scan_date`, there
    has not been time to accumulate a real trailing average — a name
    first covered eight days ago has an eight-day history, not a
    thirty-day one, and reporting a thirty-day average from it would be
    reporting a number that only looks precise.

    `earliest_known_event_date is None` means no article has ever been
    seen for this security at all (`NEVER_INGESTED`); a date that exists
    but is too recent means the window is not yet full
    (`NOT_YET_AVAILABLE`, the same reason Module 12's flags use for "not
    yet knowable"). Either way `raised` comes back `None`, never `False`.

    ## The decision, once determinable

    `baseline_mean = baseline_total / baseline_window_days` — the average
    including the zero-article days, which is what makes "normal" for a
    quiet name different from "normal" for a busy one. `raised` is
    whether today's count reaches `anomaly_multiple` times that average.
    A `baseline_mean` of exactly zero cannot be multiplied into a
    threshold, so the fallback is the plainest reading of "any coverage at
    all is unusual for a name that normally has none": `raised = today_count > 0`.
    """
    window_days = thresholds.window_days
    multiple = thresholds.multiple

    detail: dict[str, Any] = {
        "today_count": today_count,
        "baseline_total": baseline_total,
        "baseline_window_days": window_days,
        "anomaly_multiple": multiple,
        "earliest_known_event_date": (
            earliest_known_event_date.isoformat() if earliest_known_event_date else None
        ),
        "scan_date": scan_date.isoformat(),
    }

    if earliest_known_event_date is None:
        return NewsVolumeSignal(
            security_id=security_id,
            scan_date=scan_date,
            as_of=as_of,
            raised=None,
            today_count=today_count,
            baseline_mean=None,
            baseline_window_days=window_days,
            multiple_threshold=multiple,
            config_version_label=config_version_label,
            unavailable=MissReason.NEVER_INGESTED,
            detail={
                "reason": "No canonical_news row has ever been observed for this security.",
                **detail,
            },
        )

    window_start = scan_date - thresholds.window
    if earliest_known_event_date > window_start:
        days_observed = (scan_date - earliest_known_event_date).days
        return NewsVolumeSignal(
            security_id=security_id,
            scan_date=scan_date,
            as_of=as_of,
            raised=None,
            today_count=today_count,
            baseline_mean=None,
            baseline_window_days=window_days,
            multiple_threshold=multiple,
            config_version_label=config_version_label,
            unavailable=MissReason.NOT_YET_AVAILABLE,
            detail={
                "reason": (
                    f"Earliest known article is {days_observed} day(s) before this scan; "
                    f"a {window_days}-day baseline is not yet observable."
                ),
                "days_observed": days_observed,
                **detail,
            },
        )

    baseline_mean = baseline_total / window_days
    threshold = multiple * baseline_mean
    raised = today_count > 0 if baseline_mean == 0.0 else today_count >= threshold

    return NewsVolumeSignal(
        security_id=security_id,
        scan_date=scan_date,
        as_of=as_of,
        raised=raised,
        today_count=today_count,
        baseline_mean=baseline_mean,
        baseline_window_days=window_days,
        multiple_threshold=multiple,
        config_version_label=config_version_label,
        detail={
            "threshold": threshold,
            "baseline_mean": baseline_mean,
            "zero_baseline_fallback": baseline_mean == 0.0,
            **detail,
        },
    )
