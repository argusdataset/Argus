"""Pipeline health. Almost all of it is reading what Module 18 already decided.

## Module 18 built the feed; this consumes it

Module 18's report named the intended interface and the alert taxonomy in
the same breath:

> "`runs_by_status` and `DailyReport.as_dict()` are the intended feed.
> `LiveScanStatus` is already the alert taxonomy — `FAILED` wants a
> person, `DATA_NOT_READY` wants patience unless it persists past the
> readiness window, `COMPLETED_WITH_EXCLUSIONS` wants a look if the same
> security appears every day, and a `RUNNING` row with no `finished_at`
> after the window means a process died. `DailyReport.healthy` encodes the
> first-order version of that judgement."

So `scan_health` calls `runs_by_status` once per status and nothing else.
It does not query `live_scan_runs` directly, does not re-derive what
"failed" means, and does not have its own opinion about health — a
structural test asserts the first of those, because re-deriving scan
health here is exactly how a monitor starts disagreeing with the thing it
monitors.

What this module adds is only what Module 18's taxonomy names but a
single-run report cannot see, because both are questions *across* runs:

- **stuck runs** — `RUNNING` with no `finished_at` past the window. Module
  18 stated the rule; a run's own report cannot apply it, because the
  process that would have written the conclusion is the one that died.
- **persistent exclusions** — the same security excluded on many days.
  Module 18 said this "wants a look if the same security appears every
  day"; that judgement needs the set of days, which no one day has.

Neither is a new health metric. Both are the counting step Module 18's
own sentences imply.

## Ingestion health reads recorded provenance, and makes no calls

Module 04's ingestion is observed through what it wrote — the four PIT
columns Module 03 made non-nullable on every canonical table. The lag
between `event_time` and `availability_time` is how long ARGUS took to
learn something it could have known; that is a provider-and-pipeline
measurement, available from rows already on disk, requiring no request to
anybody. Nothing here contacts FMP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Table, and_, func, select
from sqlalchemy.engine import Connection

from core.live_scanner.runs import ScanRun, runs_by_status
from infra.db.enums import LiveScanStatus
from infra.observability.config import ObservabilitySettings
from infra.observability.freshness import FEEDS

__all__ = [
    "IngestionLag",
    "ScanHealth",
    "ingestion_health",
    "scan_health",
    "stuck_runs",
]


@dataclass(frozen=True, slots=True)
class ScanHealth:
    """Live-scan health, assembled from Module 18's own statuses."""

    as_of: datetime
    #: Count per `LiveScanStatus`, every status present even at zero. A
    #: missing key and a zero are different facts and only one of them is
    #: readable from an absent key.
    counts: dict[str, int]
    failed: list[dict[str, Any]] = field(default_factory=list)
    stuck: list[dict[str, Any]] = field(default_factory=list)
    #: Securities excluded on more than one day, worst first. Module 18's
    #: "wants a look if the same security appears every day".
    persistent_exclusions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """A FAILED run or a died process wants a person. Nothing else does.

        Deliberately the same judgement `DailyReport.healthy` makes, one
        level up: DATA_NOT_READY is patience, exclusions are a look, and
        neither is an incident.
        """
        return not self.failed and not self.stuck

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "healthy": self.healthy,
            "counts": self.counts,
            "failed": self.failed,
            "stuck": self.stuck,
            "persistent_exclusions": self.persistent_exclusions,
        }


def scan_health(
    connection: Connection,
    *,
    now: datetime | None = None,
    settings: ObservabilitySettings | None = None,
) -> ScanHealth:
    """Live-scan health, read entirely through Module 18's `runs_by_status`."""
    settings = settings or ObservabilitySettings()
    moment = now or datetime.now(UTC)
    limit = int(settings.max_rows_sampled)

    by_status: dict[LiveScanStatus, list[ScanRun]] = {
        status: runs_by_status(connection, status, limit=limit) for status in LiveScanStatus
    }

    running = by_status.get(LiveScanStatus.RUNNING, [])
    return ScanHealth(
        as_of=moment,
        counts={status.value: len(runs) for status, runs in by_status.items()},
        failed=[run.as_dict() for run in by_status.get(LiveScanStatus.FAILED, [])],
        stuck=[run.as_dict() for run in stuck_runs(running, now=moment, settings=settings)],
        persistent_exclusions=_persistent_exclusions(by_status),
    )


def stuck_runs(
    running: list[ScanRun],
    *,
    now: datetime | None = None,
    settings: ObservabilitySettings | None = None,
) -> list[ScanRun]:
    """RUNNING rows old enough that the process is gone.

    Module 18's sentence with a number attached. Pure — it takes the runs
    rather than fetching them — so the rule can be tested without a
    database and so `scan_health` stays the only thing that queries.

    A run is judged by its `as_of` — the session close it was scanning —
    rather than by a start timestamp. `live_scan_runs` has a `started_at`
    column, but Module 18's `ScanRun` does not expose it, and reaching
    past the feed into the table to get it is exactly the re-derivation
    this module is not supposed to do. `as_of` is on the feed, is always
    at or before the real start, and is recorded before the work begins,
    so it cannot drift the way a `now()` default can.

    The cost is stated rather than hidden: a run that started late is
    judged from when it *should* have started, so the window is measured
    generously against the scanner. That is the right direction for an
    alert — it fires late rather than falsely.
    """
    settings = settings or ObservabilitySettings()
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(seconds=settings.scan_run_stuck_after_seconds)

    return [run for run in running if run.finished_at is None and run.as_of < cutoff]


def _persistent_exclusions(
    by_status: dict[LiveScanStatus, list[ScanRun]],
) -> list[dict[str, Any]]:
    """Securities excluded on more than one day, worst first.

    Counted across the runs Module 18 handed back, not queried separately.
    A security excluded once is a bad day for that security; the same one
    excluded every day is a security ARGUS is quietly never looking at,
    which is the failure that hides inside a green dashboard.
    """
    tally: dict[str, int] = {}
    dates: dict[str, list[str]] = {}
    for runs in by_status.values():
        for run in runs:
            for excluded in run.excluded_securities or ():
                key = str(excluded)
                tally[key] = tally.get(key, 0) + 1
                dates.setdefault(key, []).append(run.scan_date.isoformat())

    return [
        {"security_id": key, "days": count, "scan_dates": sorted(dates[key])}
        for key, count in sorted(tally.items(), key=lambda item: (-item[1], item[0]))
        if count > 1
    ]


@dataclass(frozen=True, slots=True)
class IngestionLag:
    """How long ARGUS took to learn what one feed already knew."""

    feed: str
    rows: int
    #: Seconds between an event happening and ARGUS being able to see it.
    #: `None` when there are no rows — never zero.
    median_lag_seconds: float | None
    max_lag_seconds: float | None
    latest_ingestion: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "feed": self.feed,
            "rows": self.rows,
            "median_lag_seconds": self.median_lag_seconds,
            "max_lag_seconds": self.max_lag_seconds,
            "latest_ingestion": (
                self.latest_ingestion.isoformat() if self.latest_ingestion else None
            ),
        }


def ingestion_health(
    connection: Connection,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
    settings: ObservabilitySettings | None = None,
) -> list[IngestionLag]:
    """Per-feed ingestion lag, from provenance already on disk.

    No provider is contacted. The measurement is `availability_time -
    event_time` over rows ARGUS already holds: how long after something
    happened ARGUS could act on it. A provider that publishes promptly and
    a pipeline that ingests promptly are indistinguishable here and that
    is fine — this answers "is ARGUS learning things in time", and the
    split between provider and pipeline is a question for whoever the
    answer is no for.
    """
    settings = settings or ObservabilitySettings()
    moment = now or datetime.now(UTC)
    window_start = since or moment - timedelta(days=float(settings.anomaly_window_days))

    return [_lag(connection, name, FEEDS[name], window_start) for name in sorted(FEEDS)]


def _lag(connection: Connection, feed: str, table: Table, window_start: datetime) -> IngestionLag:
    lag = func.extract("epoch", table.c.availability_time - table.c.event_time)
    row = connection.execute(
        select(
            func.count().label("rows"),
            func.percentile_cont(0.5).within_group(lag).label("median"),
            func.max(lag).label("worst"),
            func.max(table.c.ingestion_time).label("latest"),
        ).where(
            and_(
                table.c.ingestion_time >= window_start,
                table.c.availability_time.is_not(None),
            )
        )
    ).one()

    rows = int(row.rows or 0)
    return IngestionLag(
        feed=feed,
        rows=rows,
        # None rather than 0.0 when nothing arrived: no lag was measured,
        # which is not the same as a lag of zero.
        median_lag_seconds=float(row.median) if rows and row.median is not None else None,
        max_lag_seconds=float(row.worst) if rows and row.worst is not None else None,
        latest_ingestion=row.latest,
    )
