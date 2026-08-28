"""Retention: how long anything ARGUS writes is kept, and who enforces it.

Module 24's handover listed two separate items that look like one problem
and are not:

- *Log retention is undefined.*
- *`audit_log`, `login_attempts` and `registration_attempts` grow
  unbounded.*

They land in different places with different mechanisms, and the useful
thing this module does is say which is which rather than implement one
policy over both.

## Where logs land, and the decision that follows

Module 23 writes one JSON object per line to **stderr** and nowhere else
— no file, no rotation, no log server. On Railway that stream is captured
by the platform and kept for a window the platform defines; ARGUS cannot
configure it and should not pretend to.

So the decision, stated plainly: **application logs are diagnostic and
disposable.** They are for finding out what happened in the last few
days. Anything that has to survive longer than the platform's window —
who logged in, who changed what, what a lockout counted — is written to
`audit_log`, which is in the database, guarded append-only, and inside
the backup.

That is a real constraint on the rest of the system, not a description of
one: it means a compliance question, an incident reconstruction, or a
"who deleted this" must be answerable from `audit_log` alone. If
something is only ever logged, it is not retained.

## Why the three growing tables cannot simply be pruned

All three are in `APPEND_ONLY_TABLES`, so a `DELETE` against any of them
raises SQLSTATE 23001 from a trigger. The obvious retention job — delete
rows older than N days — cannot run, and making it run means dropping the
guard.

Dropping the guard is not a workaround, it is the loss of the property.
`login_attempts` is the table Module 22's lockout counts; a lockout whose
evidence can be deleted is not a lockout. `registration_attempts` is the
same for Module 24's per-source signup limit. `audit_log` is the record
that survives the logs. A retention job holding the one privilege that
defeats all three is a worse risk than the disk it saves.

So retention on those tables is **measurement plus a stated migration
path**, and this module implements the measurement: `measure_growth`
reports real row counts, real on-disk bytes, the observed arrival rate
and how long until each crosses a threshold. That turns "grows unbounded"
into a number with a date attached, which is what makes it possible to
act before it matters rather than after.

The migration path, when a number here says it is time, is PostgreSQL
declarative partitioning by month: the guard triggers stay on the parent,
old data leaves by `DETACH PARTITION` rather than by `DELETE`, and the
detached table can be archived. It is deliberately not implemented now.
It is a schema change to three of Module 03's and Module 22's tables, on
behalf of a projection rather than an observation, in a database with
zero production rows — and the README's "before going live" list is a
better place for it than a migration nobody can test against real
volume.

## The one table where a delete job is both legal and right

`sessions` is not guarded, is referenced by no foreign key, and holds
rows that stop meaning anything the moment they expire. Deleting an
expired session is not erasing history — the history is in
`login_attempts` and `audit_log`, which are the tables that keep it.

So this module does prune that one, on a schedule, with a grace period
past expiry so a support question about "I was logged out yesterday" is
still answerable from the row rather than only from the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Engine, delete, func, select, text

from infra.db.connection import create_db_engine
from infra.db.schema.users import (
    audit_log,
    login_attempts,
    registration_attempts,
    sessions,
)
from infra.observability.logging import configure_logging, get_logger

__all__ = [
    "GROWTH_WARNING_BYTES",
    "MONITORED_TABLES",
    "POLICIES",
    "SESSION_RETENTION_DAYS",
    "RetentionPolicy",
    "TableGrowth",
    "main",
    "measure_growth",
    "prune_expired_sessions",
    "run_retention",
]

_log = get_logger("argus.deploy.retention")

RetentionClass = Literal["prunable", "immutable", "platform"]

#: How long an expired session row is kept before it is deleted.
#: Operational, not a security control — the session stopped being usable
#: at `expires_at`, which Module 22 enforces on every request. This is
#: only how long the record of it stays around to answer a question.
SESSION_RETENTION_DAYS = 30

#: On-disk size at which a monitored append-only table stops being
#: something to watch and becomes something to act on. One gibibyte is
#: not a limit Postgres cares about — it is the point at which a
#: `pg_dump` of the table starts to lengthen the restore drill
#: measurably, and restore time is the constraint that actually binds
#: here (see the README's RTO).
GROWTH_WARNING_BYTES = 1024**3

#: The append-only tables whose growth has no ceiling, with the column
#: that dates a row. Every one of these is written on a path an anonymous
#: caller can trigger, which is what makes the growth unbounded rather
#: than merely large.
MONITORED_TABLES: dict[str, str] = {
    "audit_log": "occurred_at",
    "login_attempts": "attempted_at",
    "registration_attempts": "attempted_at",
}

_TABLES = {
    "audit_log": audit_log,
    "login_attempts": login_attempts,
    "registration_attempts": registration_attempts,
}


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """One declared retention decision, and why it is that one."""

    name: str
    retention_class: RetentionClass
    retain_days: int | None
    enforced_by: str
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "retention_class": self.retention_class,
            "retain_days": self.retain_days,
            "enforced_by": self.enforced_by,
        }


#: Every place ARGUS writes something that accumulates. Enumerable on
#: purpose: "what is our retention policy" should be answerable by
#: reading one object, and a new table that accumulates should be
#: obviously missing from it.
POLICIES: tuple[RetentionPolicy, ...] = (
    RetentionPolicy(
        name="application_logs",
        retention_class="platform",
        retain_days=None,
        enforced_by="Railway log retention",
        rationale=(
            "JSON on stderr, captured by the platform. ARGUS does not control the "
            "window, so nothing that must outlive it is only logged."
        ),
    ),
    RetentionPolicy(
        name="sessions",
        retention_class="prunable",
        retain_days=SESSION_RETENTION_DAYS,
        enforced_by="infra.deploy.retention.prune_expired_sessions",
        rationale=(
            "Unguarded and referenced by nothing. An expired session row carries no "
            "history that login_attempts and audit_log do not already hold."
        ),
    ),
    RetentionPolicy(
        name="audit_log",
        retention_class="immutable",
        retain_days=None,
        enforced_by="append-only trigger; monitored by measure_growth",
        rationale=(
            "The record that outlives the logs. Kept forever because the alternative "
            "to keeping it is having no durable record at all."
        ),
    ),
    RetentionPolicy(
        name="login_attempts",
        retention_class="immutable",
        retain_days=None,
        enforced_by="append-only trigger; monitored by measure_growth",
        rationale=(
            "Module 22 counts failures here to lock an account out. A lockout whose "
            "evidence can be deleted is not a lockout."
        ),
    ),
    RetentionPolicy(
        name="registration_attempts",
        retention_class="immutable",
        retain_days=None,
        enforced_by="append-only trigger; monitored by measure_growth",
        rationale=(
            "Module 24's per-source signup limit counts these. Same reasoning as "
            "login_attempts, applied to registration."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class TableGrowth:
    """What one append-only table currently costs, and how fast that grows."""

    table: str
    rows: int
    bytes_on_disk: int
    oldest: datetime | None
    newest: datetime | None
    rows_per_day: float
    bytes_per_day: float
    days_to_warning: float | None

    @property
    def over_threshold(self) -> bool:
        return self.bytes_on_disk >= GROWTH_WARNING_BYTES

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "rows": self.rows,
            "bytes_on_disk": self.bytes_on_disk,
            "rows_per_day": round(self.rows_per_day, 3),
            "bytes_per_day": round(self.bytes_per_day, 1),
            "days_to_warning": (
                None if self.days_to_warning is None else round(self.days_to_warning, 1)
            ),
            "over_threshold": self.over_threshold,
        }


def prune_expired_sessions(
    engine: Engine,
    *,
    now: datetime | None = None,
    retain_days: int = SESSION_RETENTION_DAYS,
) -> int:
    """Delete sessions that expired longer than `retain_days` ago.

    Keyed on `expires_at` rather than `revoked_at`: a revoked session is
    already unusable, but its row still dates from a real login and the
    expiry is the point after which it can tell nobody anything new. A
    session revoked on day one and expiring on day thirty is kept until
    day sixty, which is the conservative direction to be wrong in.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=retain_days)

    with engine.begin() as connection:
        result = connection.execute(delete(sessions).where(sessions.c.expires_at < cutoff))
    removed = result.rowcount or 0

    _log.info(
        "expired sessions pruned",
        extra={
            "event": "sessions_pruned",
            "removed": removed,
            "cutoff": cutoff.isoformat(),
            "retain_days": retain_days,
        },
    )
    return removed


def measure_growth(
    engine: Engine,
    *,
    now: datetime | None = None,
) -> tuple[TableGrowth, ...]:
    """Current size and observed arrival rate for each monitored table.

    The rate is measured from the data rather than assumed: rows divided
    by the span between the oldest and newest row. That is the honest
    number early on — a table holding one day of a load test will project
    alarmingly, and it *should*, because that is what that arrival rate
    means if it continues.

    A table with fewer than two rows, or whose rows all landed inside one
    day, gets `days_to_warning=None`. There is no rate to extrapolate
    from and inventing one would be worse than saying so.
    """
    moment = now or datetime.now(UTC)
    measurements: list[TableGrowth] = []

    with engine.connect() as connection:
        for name, column in MONITORED_TABLES.items():
            table = _TABLES[name]
            stamp = table.c[column]

            rows, oldest, newest = connection.execute(
                select(func.count(), func.min(stamp), func.max(stamp)).select_from(table)
            ).one()

            size = connection.execute(
                text("SELECT pg_total_relation_size(CAST(:name AS regclass))"),
                {"name": name},
            ).scalar_one()

            measurements.append(_growth(name, rows, int(size), oldest, newest, moment))

    for measurement in measurements:
        _log.info(
            "table growth measured",
            extra={"event": "table_growth", **measurement.as_dict()},
        )
        if measurement.over_threshold:
            _log.warning(
                "append-only table past its growth threshold",
                extra={
                    "event": "retention_threshold_crossed",
                    "table": measurement.table,
                    "bytes_on_disk": measurement.bytes_on_disk,
                    "threshold_bytes": GROWTH_WARNING_BYTES,
                },
            )

    return tuple(measurements)


def run_retention(engine: Engine, *, now: datetime | None = None) -> dict[str, Any]:
    """One retention pass: prune what can be pruned, measure what cannot."""
    moment = now or datetime.now(UTC)
    removed = prune_expired_sessions(engine, now=moment)
    growth = measure_growth(engine, now=moment)
    return {
        "sessions_removed": removed,
        "growth": [measurement.as_dict() for measurement in growth],
    }


def main(argv: list[str] | None = None) -> int:
    """The retention cron entrypoint.

    Exits non-zero only on failure. A table over its threshold is a
    warning in the log and not a failed run: the run did exactly what it
    was asked to, and failing it would turn a capacity signal into a
    deploy-shaped alarm that nothing can act on at 3am anyway.
    """
    configure_logging()
    _ = argv
    try:
        engine = create_db_engine(pool_pre_ping=True, pool_size=2, max_overflow=0)
        run_retention(engine)
    except Exception as error:  # noqa: BLE001 - the platform must see any failure
        _log.exception(
            "retention pass failed",
            extra={"event": "retention_failed", "error_type": type(error).__name__},
        )
        return 1
    return 0


def _growth(
    name: str,
    rows: int,
    size: int,
    oldest: datetime | None,
    newest: datetime | None,
    moment: datetime,
) -> TableGrowth:
    span_days = 0.0
    if oldest is not None and newest is not None:
        span_days = (newest - oldest).total_seconds() / 86_400.0

    if rows < 2 or span_days < 1.0:
        return TableGrowth(
            table=name,
            rows=rows,
            bytes_on_disk=size,
            oldest=oldest,
            newest=newest,
            rows_per_day=0.0,
            bytes_per_day=0.0,
            days_to_warning=None,
        )

    rows_per_day = rows / span_days
    bytes_per_day = size / span_days
    remaining = GROWTH_WARNING_BYTES - size

    _ = moment
    return TableGrowth(
        table=name,
        rows=rows,
        bytes_on_disk=size,
        oldest=oldest,
        newest=newest,
        rows_per_day=rows_per_day,
        bytes_per_day=bytes_per_day,
        days_to_warning=max(remaining, 0) / bytes_per_day if bytes_per_day else None,
    )


if __name__ == "__main__":  # pragma: no cover - the cron entrypoint
    raise SystemExit(main())
