"""The health-check surface. Cheap, honest, and degraded rather than down.

## Three states, not two

`ok` / `degraded` / `down`. A binary health check forces a false choice:
a database that is unreachable and a feed that is a day late both become
"unhealthy", and an operator woken at 3am cannot tell which they have
until they look. So:

- **down** — a dependency ARGUS cannot work without is unreachable. The
  database, or a schema that is not the one the code expects.
- **degraded** — ARGUS is serving, and something wants a person. A stale
  feed, a FAILED scan, a died process, a state-machine gap.
- **ok** — nothing outstanding.

The distinction matters for the thing consuming this: a load balancer
should take a `down` instance out of rotation and must **not** take a
`degraded` one out, because a stale feed is not fixed by having fewer
servers and removing them makes the outage worse.

## What is checked, and what it costs

Every check is one indexed query or less. A health endpoint that gets
slow under load fails exactly when it is needed, so `checks` never scans
a large table and never computes an aggregate over the full history —
the expensive reads live on the individual monitors, which a person calls
deliberately.

Migration status is checked by reading `alembic_version` and comparing it
to the revision the code expects. A process running against a database
one migration behind is the failure that produces confusing errors
everywhere else, and it is one row to rule out.

## Nothing here notifies anybody

Explicitly out of scope. This produces a structured verdict; delivering
it is a future system's job, and building half of one here would leave
ARGUS with a notifier nobody configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from infra.observability.config import ObservabilitySettings
from infra.observability.freshness import all_feeds

__all__ = ["Check", "HealthReport", "Status", "check_health", "expected_revision"]


class Status(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


#: Rank so a report's overall status is the worst of its parts.
_RANK: dict[Status, int] = {Status.OK: 0, Status.DEGRADED: 1, Status.DOWN: 2}


@dataclass(frozen=True, slots=True)
class Check:
    """One dependency's verdict, and enough detail to act on it."""

    name: str
    status: Status
    detail: str
    #: Structured facts a tool can read. Never a credential — this
    #: response is the one most likely to be exposed unauthenticated.
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "data": self.data,
        }


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Every check, and the worst verdict among them."""

    as_of: datetime
    checks: list[Check]

    @property
    def status(self) -> Status:
        return max(
            (check.status for check in self.checks), key=lambda s: _RANK[s], default=Status.OK
        )

    @property
    def healthy(self) -> bool:
        return self.status is Status.OK

    #: What an HTTP surface should return. 503 only for `down`: a degraded
    #: instance is still serving and taking it out of rotation makes the
    #: problem worse rather than better.
    @property
    def http_status(self) -> int:
        return 503 if self.status is Status.DOWN else 200

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "status": self.status.value,
            "healthy": self.healthy,
            "checks": [check.as_dict() for check in self.checks],
        }


def expected_revision() -> str:
    """The migration revision this code was written against.

    Read from the migration files rather than hardcoded, so adding a
    migration cannot leave this pointing at the previous head — which
    would make the check pass on a database the code cannot actually use.
    """
    from pathlib import Path

    versions = Path(__file__).resolve().parents[1] / "db" / "migrations" / "versions"
    revisions: set[str] = set()
    down: set[str] = set()
    for path in versions.glob("[0-9]*.py"):
        source = path.read_text()
        for line in source.splitlines():
            if line.startswith("revision: str = "):
                revisions.add(line.split("=", 1)[1].strip().strip("\"'"))
            elif line.startswith("down_revision: str | None = "):
                value = line.split("=", 1)[1].strip().strip("\"'")
                if value != "None":
                    down.add(value)
    heads = revisions - down
    return sorted(heads)[-1] if heads else ""


def check_health(
    engine: Engine | None = None,
    *,
    connection: Connection | None = None,
    now: datetime | None = None,
    settings: ObservabilitySettings | None = None,
) -> HealthReport:
    """Run every check. Never raises — a health check that throws is down.

    Takes an engine or an already-open connection. The engine form is what
    a service uses; the connection form is what a caller inside a
    transaction uses, and is the only way this can be tested without
    committing.
    """
    settings = settings or ObservabilitySettings()
    moment = now or datetime.now(UTC)

    if connection is not None:
        return HealthReport(as_of=moment, checks=_checks(connection, moment, settings))

    if engine is None:
        return HealthReport(
            as_of=moment,
            checks=[
                Check(
                    name="database",
                    status=Status.DOWN,
                    detail="No engine and no connection was supplied to the health check.",
                )
            ],
        )

    try:
        with engine.connect() as opened:
            return HealthReport(as_of=moment, checks=_checks(opened, moment, settings))
    except SQLAlchemyError as error:
        return HealthReport(
            as_of=moment,
            checks=[
                Check(
                    name="database",
                    status=Status.DOWN,
                    detail="The database could not be reached.",
                    # The exception type, never the message: a connection
                    # error commonly contains the DSN, and the DSN
                    # commonly contains a password. This response is the
                    # one most likely to be served unauthenticated.
                    data={"error": type(error).__name__},
                )
            ],
        )


def _checks(
    connection: Connection, moment: datetime, settings: ObservabilitySettings
) -> list[Check]:
    checks = [_database(connection), _migrations(connection)]
    if checks[0].status is Status.DOWN or checks[1].status is Status.DOWN:
        # A database that is unreachable or on the wrong schema makes
        # every other check meaningless — and, worse, makes them fail in
        # ways that read as separate problems.
        return checks
    checks.append(_feeds(connection, moment, settings))
    return checks


def _database(connection: Connection) -> Check:
    try:
        connection.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        return Check(
            name="database",
            status=Status.DOWN,
            detail="The database did not answer.",
            data={"error": type(error).__name__},
        )
    return Check(name="database", status=Status.OK, detail="Reachable.")


def _migrations(connection: Connection) -> Check:
    expected = expected_revision()
    try:
        current = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
    except SQLAlchemyError as error:
        return Check(
            name="migrations",
            status=Status.DOWN,
            detail="No alembic_version table — this database has never been migrated.",
            data={"expected": expected, "error": type(error).__name__},
        )

    if current is None:
        return Check(
            name="migrations",
            status=Status.DOWN,
            detail="alembic_version is empty; the schema is not established.",
            data={"expected": expected},
        )
    if current != expected:
        return Check(
            name="migrations",
            status=Status.DOWN,
            detail=(
                f"The database is at revision {current} and this code expects "
                f"{expected}. Running against a schema the code does not match "
                "produces confusing failures everywhere else."
            ),
            data={"current": current, "expected": expected},
        )
    return Check(
        name="migrations",
        status=Status.OK,
        detail=f"At revision {current}.",
        data={"current": current, "expected": expected},
    )


def _feeds(connection: Connection, moment: datetime, settings: ObservabilitySettings) -> Check:
    feeds = all_feeds(connection, now=moment, settings=settings)
    unhealthy = [feed for feed in feeds if not feed.healthy]
    if not unhealthy:
        return Check(
            name="data_freshness",
            status=Status.OK,
            detail="Every watched feed is fresh or explicably delayed.",
            data={feed.feed: feed.state.value for feed in feeds},
        )
    return Check(
        name="data_freshness",
        status=Status.DEGRADED,
        detail=(
            f"{len(unhealthy)} of {len(feeds)} feeds are stale or have never delivered. "
            "ARGUS is still serving, with ages attached."
        ),
        data={feed.feed: feed.state.value for feed in feeds},
    )
