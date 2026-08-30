"""Materializing the charts, and refusing to serve one that has been withdrawn.

## The approach: materialized, refreshed explicitly, verified on read

The public page is the one surface in ARGUS expected to take real traffic
with no login. Three options were available and the trade-offs are not
close:

- **On demand.** Every request loads every published outcome and computes
  every chart. Correct, trivially fresh, and the wrong shape: the
  work scales with published history and is repeated identically for
  every visitor.
- **Postgres materialized views.** Cheap reads, but the gate is not
  expressible as a view predicate — it depends on `approved_runs()` and
  `approved_windows()`, both of which are window-function queries over
  status logs — and a `REFRESH MATERIALIZED VIEW` has nowhere to record
  *which* approvals it was computed under, which is the property the next
  section shows is essential.
- **A snapshot table, refreshed explicitly.** Taken. A read is one
  indexed lookup of a stored JSONB payload. Refresh is a deliberate call,
  which means the cadence is an operational decision rather than a
  property of traffic.

## Why materializing is safe here, and would not be without the fingerprint

Materializing a public statistic has a specific failure mode: a result is
published, later withdrawn, and the stored copy keeps serving it until
somebody remembers to refresh. For a page whose entire purpose is to be
checkable, that is worse than being slow.

So every snapshot records the **exact** set of approved runs and windows
it drew from, plus a hash of that set. On read, the current gate state is
recomputed — two indexed queries — and compared:

- **Fingerprints match** → serve.
- **Current scope still covers the snapshot's** → something was
  *added*. Every number in the snapshot is still true, merely incomplete.
  Served, flagged `stale`, with the reason stated.
- **Otherwise** → something was *removed*. A run or window that this
  payload counted is no longer approved, so the payload contains
  withdrawn results. **Refused**, with `STATISTICS_WITHDRAWN`.

That last case takes the page down until a refresh runs, and that is the
intended trade. ARGUS's stated position is that its public credibility
depends on never quietly inflating its track record; an error saying
"these figures are being recomputed" honours that and a stale percentage
does not.

## Freshness is reported, not enforced

`expected_refresh_hours` marks a snapshot stale, it does not expire it. A
day-old true number beats a spinner, and every response carries
`computed_at`, `age_seconds` and `as_of` so a reader can judge the
staleness themselves rather than having ARGUS judge it for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, desc, select
from sqlalchemy.engine import Connection

from infra.db.schema.public_stats import public_stat_snapshots
from services.public_stats.aggregates import CHARTS, build_chart
from services.public_stats.config import PublicStatsConfig
from services.public_stats.errors import (
    STATISTICS_UNAVAILABLE,
    STATISTICS_WITHDRAWN,
    PublicStatsError,
)
from services.public_stats.gate import PublishScope, current_scope, published_dataset

__all__ = [
    "StoredChart",
    "read_chart",
    "refresh_public_stats",
]


@dataclass(frozen=True, slots=True)
class StoredChart:
    """One materialized chart, with everything a reader needs to judge it."""

    chart: str
    payload: dict[str, Any]
    computed_at: datetime
    as_of: datetime
    sample_size: int
    included_runs: list[str]
    included_windows: list[dict[str, str]]
    stale: bool
    staleness_reason: str | None = None

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(UTC)) - self.computed_at).total_seconds()

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            **self.payload,
            "freshness": {
                "computed_at": self.computed_at.isoformat(),
                "as_of": self.as_of.isoformat(),
                "age_seconds": self.age_seconds(now),
                "stale": self.stale,
                "staleness_reason": self.staleness_reason,
            },
            "provenance": {
                "approved_runs": self.included_runs,
                "approved_live_windows": self.included_windows,
                "note": (
                    "Every figure here comes from validation runs a named human approved, "
                    "and from live-tracked outcomes inside an approved release window. "
                    "Nothing pending or rejected is included."
                ),
            },
        }


def refresh_public_stats(
    connection: Connection,
    *,
    as_of: datetime | None = None,
    config: PublicStatsConfig | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Recompute and store every chart. The only writer in this module.

    Loads the published dataset once and builds every chart from it,
    rather than once per chart: the load is the expensive half, the
    aggregates are cheap over an in-memory frame, and computing them from
    one dataset makes it impossible for two charts on the same page to
    disagree about how many outcomes exist.

    Returns each chart's sample size, so a scheduled refresh can log
    something more useful than "done".
    """
    config = config or PublicStatsConfig()
    moment = as_of or datetime.now(UTC)
    computed = now or datetime.now(UTC)

    scope = current_scope(connection)
    dataset = published_dataset(connection, scope, as_of=moment)
    fingerprint = scope.fingerprint()

    sizes: dict[str, int] = {}
    for chart in CHARTS:
        payload = build_chart(chart, dataset.frame, config)
        sizes[chart] = payload.sample_size

        # Replace rather than accumulate: a snapshot is a cache, and
        # keeping every historical version of a cache would turn this
        # table into a slow-growing log nobody reads. The audited record
        # of what was publishable lives in the gate tables, which are
        # append-only.
        connection.execute(
            delete(public_stat_snapshots).where(public_stat_snapshots.c.chart == chart)
        )
        connection.execute(
            public_stat_snapshots.insert().values(
                chart=chart,
                computed_at=computed,
                as_of=moment,
                gate_fingerprint=fingerprint,
                included_runs=[str(run.run_id) for run in scope.runs],
                included_windows=[window.as_dict() for window in scope.windows],
                payload=payload.as_dict(),
                sample_size=payload.sample_size,
            )
        )

    return sizes


def read_chart(
    connection: Connection,
    chart: str,
    *,
    config: PublicStatsConfig | None = None,
    now: datetime | None = None,
) -> StoredChart:
    """One chart, verified against the current gate state before serving.

    The verification is the point of this function. Reading the row is one
    indexed lookup; the two extra queries that rebuild the current scope
    are what stop a withdrawn result staying on the page.
    """
    config = config or PublicStatsConfig()
    moment = now or datetime.now(UTC)

    row = connection.execute(
        select(public_stat_snapshots)
        .where(public_stat_snapshots.c.chart == chart)
        .order_by(desc(public_stat_snapshots.c.computed_at))
        .limit(1)
    ).one_or_none()

    if row is None:
        raise PublicStatsError(
            STATISTICS_UNAVAILABLE,
            "ARGUS has not published statistics yet. This is not a failure and not a "
            "zero: no validation run has been approved and no live release window has "
            "been approved, so there is nothing ARGUS is permitted to show.",
            status=503,
            detail={"chart": chart},
        )

    scope = current_scope(connection)
    stale, reason = _staleness(row, scope, config=config, now=moment)

    return StoredChart(
        chart=row.chart,
        payload=dict(row.payload),
        computed_at=row.computed_at,
        as_of=row.as_of,
        sample_size=int(row.sample_size),
        included_runs=list(row.included_runs or []),
        included_windows=list(row.included_windows or []),
        stale=stale,
        staleness_reason=reason,
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _staleness(
    row: Any, scope: PublishScope, *, config: PublicStatsConfig, now: datetime
) -> tuple[bool, str | None]:
    """Whether this snapshot may still be served, and what to say about it.

    Raises rather than returning for the withdrawal case, because that is
    not a degree of staleness — it is a payload that must not reach a
    reader.
    """
    if row.gate_fingerprint != scope.fingerprint():
        stored = _stored_scope(row)
        if not scope.covers(stored):
            withdrawn = _withdrawn(stored, scope)
            raise PublicStatsError(
                STATISTICS_WITHDRAWN,
                "These statistics included results that are no longer approved for "
                "publication, and ARGUS will not serve a figure it has withdrawn. They "
                "will return once recomputed from what is currently approved.",
                status=503,
                detail={"chart": row.chart, "withdrawn": withdrawn},
            )
        return True, (
            "New results have been approved since these figures were computed, so they "
            "are incomplete but not wrong. Every number shown is still from approved "
            "results."
        )

    age = (now - row.computed_at).total_seconds()
    if age > config.settings.refresh_interval.total_seconds():
        return True, (
            f"Computed {int(age // 3600)} hour(s) ago, older than the "
            f"{int(config.settings.expected_refresh_hours)}-hour refresh cadence ARGUS "
            "expects. Still accurate as of the time shown."
        )
    return False, None


def _stored_scope(row: Any) -> PublishScope:
    """Rebuild the scope a stored payload was computed under.

    Only the identifying parts — this is used for set comparison, never
    for loading, which is why it does not need the periods back.
    """
    from uuid import UUID

    from services.public_stats.gate import ApprovedRunScope, ApprovedWindowScope

    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    runs = tuple(
        ApprovedRunScope(
            run_id=UUID(value),
            period_start=epoch,
            period_end=epoch,
            universe_version_id=UUID(int=0),
            data_snapshot_id=UUID(int=0),
        )
        for value in (row.included_runs or [])
    )
    windows = tuple(
        ApprovedWindowScope(
            period_start=datetime.fromisoformat(entry["period_start"]).replace(tzinfo=UTC),
            period_end=datetime.fromisoformat(entry["period_end"]).replace(tzinfo=UTC),
            data_snapshot_id=UUID(entry["data_snapshot_id"]),
        )
        for entry in (row.included_windows or [])
    )
    return PublishScope(runs=runs, windows=windows)


def _withdrawn(stored: PublishScope, current: PublishScope) -> dict[str, list[str]]:
    """What the stored payload counted that is no longer approved.

    Named rather than merely detected, so an operator reading the error
    knows which approval changed instead of having to diff two sets by
    hand.
    """
    current_runs = {run.run_id for run in current.runs}
    current_windows = {
        (w.period_start.date(), w.period_end.date(), w.data_snapshot_id) for w in current.windows
    }
    return {
        "runs": [str(run.run_id) for run in stored.runs if run.run_id not in current_runs],
        "windows": [
            f"{w.period_start.date()}..{w.period_end.date()}"
            for w in stored.windows
            if (w.period_start.date(), w.period_end.date(), w.data_snapshot_id)
            not in current_windows
        ],
    }
