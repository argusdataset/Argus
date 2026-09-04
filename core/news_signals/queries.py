"""The one aggregate query this module runs against `canonical_news`.

One query for however many securities are being assessed, not one per
security — the same discipline every batched reader in this project
follows (Module 26's `tickers_as_of`, Module 21's `_tickers`): at ten
thousand securities a day, a per-row round trip is the difference between
one query and ten thousand.

Three numbers per security, computed with SQL's `FILTER` clause so all
three come back from a single pass over the table rather than three
separate `WHERE`-bounded scans:

- `today_count` — articles whose `event_time` falls on `scan_date`'s own
  UTC calendar day.
- `baseline_total` — articles in the `baseline_window_days` before that,
  excluding today.
- `earliest_event_date` — the oldest article ARGUS could see at all for
  this security, which is what `evaluate()` uses to decide whether a
  trailing baseline is even measurable yet.

All three are bounded by `availability_time <= as_of`, the ordinary PIT
filter every canonical read in ARGUS applies — an article filed after
`as_of` must not be counted as if it had already been visible, whether it
would have landed in today's bucket or a baseline day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from core.news_signals.config import NewsSignalThresholds

__all__ = ["NewsCounts", "news_counts_for"]

_QUERY = """
    SELECT
        security_id,
        COUNT(*) FILTER (
            WHERE event_time >= :today_start AND event_time < :today_end
        ) AS today_count,
        COUNT(*) FILTER (
            WHERE event_time >= :window_start AND event_time < :today_start
        ) AS baseline_total,
        MIN(event_time) AS earliest_event_time
    FROM canonical_news
    WHERE security_id = ANY(:security_ids)
      AND availability_time <= :as_of
    GROUP BY security_id
"""


@dataclass(frozen=True, slots=True)
class NewsCounts:
    """The raw counts one security contributed, before any decision."""

    today_count: int
    baseline_total: int
    earliest_event_date: date | None


def news_counts_for(
    connection: Connection,
    security_ids: list[UUID],
    *,
    scan_date: date,
    as_of: datetime,
    thresholds: NewsSignalThresholds,
) -> dict[UUID, NewsCounts]:
    """Today's count, the baseline total, and the earliest known article.

    A security with **no** `canonical_news` row at all — knowable or
    not — is absent from the result entirely, because `GROUP BY` has
    nothing to group. Callers must treat a missing key the same as
    `NewsCounts(0, 0, None)`; `evaluate()` reads `None` as "never
    ingested" either way, so this is documented rather than papered over
    with a manufactured zero-row.
    """
    if not security_ids:
        return {}

    today_start = datetime.combine(scan_date, time.min, tzinfo=UTC)
    today_end = today_start + timedelta(days=1)
    window_start = today_start - thresholds.window

    rows = connection.execute(
        text(_QUERY),
        {
            "security_ids": security_ids,
            "today_start": today_start,
            "today_end": today_end,
            "window_start": window_start,
            "as_of": as_of,
        },
    ).all()

    return {
        row.security_id: NewsCounts(
            today_count=int(row.today_count),
            baseline_total=int(row.baseline_total),
            earliest_event_date=(
                row.earliest_event_time.astimezone(UTC).date()
                if row.earliest_event_time is not None
                else None
            ),
        )
        for row in rows
    }
