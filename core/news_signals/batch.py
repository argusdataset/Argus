"""Computing and storing one day's readings for many securities at once.

Two functions, deliberately kept separate: `assess_batch` is pure once the
one aggregate query has run, and `store_signals` is the only place this
module writes to the database. A caller that only wants to look, without
persisting anything, can call the first and stop.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.news_signals.config import NewsSignalConfig
from core.news_signals.queries import NewsCounts, news_counts_for
from core.news_signals.signal import NewsVolumeSignal, evaluate
from infra.db.schema.news_signals import news_volume_signals

#: Rows per upsert statement. Matches `data/normalization/persistence.py`'s
#: `DEFAULT_BATCH_SIZE` — large enough that a full-universe run is not
#: dominated by round trips, small enough to keep one statement's size
#: bounded regardless of how large the universe grows.
DEFAULT_BATCH_SIZE = 1_000

__all__ = ["DEFAULT_BATCH_SIZE", "assess_batch", "store_signals"]


def assess_batch(
    connection: Connection,
    security_ids: list[UUID],
    *,
    scan_date: date,
    as_of: datetime,
    config: NewsSignalConfig | None = None,
) -> dict[UUID, NewsVolumeSignal]:
    """Every security's reading for `scan_date`, from one aggregate query."""
    if not security_ids:
        return {}

    resolved = config or NewsSignalConfig()
    version_label = resolved.version_label()
    counts = news_counts_for(
        connection,
        security_ids,
        scan_date=scan_date,
        as_of=as_of,
        thresholds=resolved.thresholds,
    )

    empty = NewsCounts(today_count=0, baseline_total=0, earliest_event_date=None)
    return {
        security_id: evaluate(
            security_id=security_id,
            scan_date=scan_date,
            as_of=as_of,
            today_count=counts.get(security_id, empty).today_count,
            baseline_total=counts.get(security_id, empty).baseline_total,
            earliest_known_event_date=counts.get(security_id, empty).earliest_event_date,
            thresholds=resolved.thresholds,
            config_version_label=version_label,
        )
        for security_id in security_ids
    }


def store_signals(
    connection: Connection,
    signals: list[NewsVolumeSignal],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Upsert one day's readings. A rerun of the same day overwrites it.

    `(security_id, signal_date)` is unique, so this is the same
    `ON CONFLICT DO UPDATE` shape `core/market_state/transitions.py` uses
    to upsert the `market_state` projection — a same-day rerun converges
    on one correct row rather than accumulating several.
    """
    written = 0
    for start in range(0, len(signals), batch_size):
        batch = signals[start : start + batch_size]
        if not batch:
            continue
        statement = insert(news_volume_signals).values(
            [
                {
                    "security_id": signal.security_id,
                    "signal_date": signal.scan_date,
                    "raised": signal.raised,
                    "today_count": signal.today_count,
                    "baseline_mean": signal.baseline_mean,
                    "baseline_window_days": signal.baseline_window_days,
                    "multiple_threshold": signal.multiple_threshold,
                    "unavailable_reason": (
                        signal.unavailable.value if signal.unavailable else None
                    ),
                    "config_version_label": signal.config_version_label,
                    "computed_at": signal.as_of,
                    "detail": signal.detail,
                }
                for signal in batch
            ]
        )
        result = connection.execute(
            statement.on_conflict_do_update(
                constraint="uq_news_volume_signal_security_date",
                set_={
                    "raised": statement.excluded.raised,
                    "today_count": statement.excluded.today_count,
                    "baseline_mean": statement.excluded.baseline_mean,
                    "baseline_window_days": statement.excluded.baseline_window_days,
                    "multiple_threshold": statement.excluded.multiple_threshold,
                    "unavailable_reason": statement.excluded.unavailable_reason,
                    "config_version_label": statement.excluded.config_version_label,
                    "computed_at": statement.excluded.computed_at,
                    "detail": statement.excluded.detail,
                },
            ).returning(news_volume_signals.c.id)
        )
        written += len(result.fetchall())
    return written
