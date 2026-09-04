"""One security's reading. A thin wrapper over the batch path.

Exists for callers that genuinely want one security — a replay, an ad hoc
check, a test — without duplicating the SQL in `queries.py`. The daily
batch (`orchestrator.py`) calls the batch function directly instead,
because doing that for ten thousand securities one at a time would be
exactly the round-trip cost this module's own docstring warns against.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy.engine import Connection

from core.news_signals.config import NewsSignalConfig
from core.news_signals.queries import NewsCounts, news_counts_for
from core.news_signals.signal import NewsVolumeSignal, evaluate

__all__ = ["assess_news_volume"]


def assess_news_volume(
    connection: Connection,
    security_id: UUID,
    *,
    scan_date: date,
    as_of: datetime,
    config: NewsSignalConfig | None = None,
) -> NewsVolumeSignal:
    """This security's news-volume reading for `scan_date`, computed fresh.

    `scan_date` and `as_of` are both plain arguments, never derived here —
    the same rule Module 12's `assess_risk_context` follows, for the same
    reason: a replay of a past date has to be able to ask this exact
    question about that date.
    """
    resolved = config or NewsSignalConfig()
    counts = news_counts_for(
        connection,
        [security_id],
        scan_date=scan_date,
        as_of=as_of,
        thresholds=resolved.thresholds,
    ).get(security_id, NewsCounts(today_count=0, baseline_total=0, earliest_event_date=None))

    return evaluate(
        security_id=security_id,
        scan_date=scan_date,
        as_of=as_of,
        today_count=counts.today_count,
        baseline_total=counts.baseline_total,
        earliest_known_event_date=counts.earliest_event_date,
        thresholds=resolved.thresholds,
        config_version_label=resolved.version_label(),
    )
