"""The Live Scanner as a deployable process. A different shape from the APIs.

Module 18 built `run_daily(connect, lineage=...)`: idempotent, safe to
call repeatedly, catching up whatever dates are outstanding oldest-first.
It takes a connection *factory* rather than a connection, and a
`Lineage` rather than constructing one. Both of those are what makes it
testable, and both are what this file has to supply for real.

## Why this is not one of the API services

An API process serves requests and is measured by latency; a scanner
process wakes up, does an unbounded amount of work against fifteen years
of bars, and is measured by whether it finished. Putting them in one
process would mean a scan's memory footprint sitting in the same
container as the request path, and an autoscaler moving the wrong number.

On Railway that difference is concrete: the API services are `web`
services with a health check and a public domain; the scanner is a `cron`
service with a schedule and no port. Same image (see the Dockerfile on
why), different command, different lifecycle.

## Idempotence is what makes a cron schedule safe

`run_daily` is safe to call repeatedly — Module 18's own words — and
Module 18's `live_scan_runs` table records an attempt per date with a
monotonic `attempt` counter. So a schedule that fires while a previous
run is still going, or fires twice because the platform retried, does not
corrupt anything: the second run finds the dates already done and does
nothing. That property is why this can be a plain cron entry rather than
a distributed lock.

## Lineage: the one thing this process cannot manufacture

`Lineage` has six parts. Five are derivable here:

- Four are **published from code** — `publish_target_model_version`,
  `publish_feature_schema_version`, `publish_scoring_configuration`,
  `publish_detection_configuration` — each idempotent by content
  checksum, so calling them on every scanner start either finds the
  existing row or creates the one this code's configuration implies.
  That is exactly right: the lineage a scan records should be the
  lineage of the code that ran it.
- `data_snapshot_id` likewise, via `publish_outcome_snapshot`.

The sixth, `universe_version_id`, is **not derivable from code**. A
universe version is the output of Module 06 running against a real
provider — a fact about the market on a date, not a property of this
build. So this process cannot create one, and must find one.

**And finding one is a "latest row wins" question**, which is exactly the
hazard Module 23 catalogued and Module 24 was told not to fix.
`universe_version.created_at` defaults to `now()`, its primary key is a
random UUID, and `ORDER BY created_at DESC LIMIT 1` over two versions
published in one transaction is a coin flip. So this file does not write
that query. `resolve_universe_version` requires the version to be named
explicitly — `ARGUS_UNIVERSE_VERSION` — and refuses to guess when it is
not, with an error saying which versions exist.

That is a deliberate operational cost: somebody has to set a variable
after building a universe. It buys the property that a production scan's
lineage was chosen by a person rather than by whichever row a
non-deterministic sort happened to return, and it adds no new instance of
a hazard this project has now found four times.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.engine import Connection

from core.candidate_detection.config import publish_detection_configuration
from core.feature_engine.spec import publish_feature_schema_version
from core.live_scanner.daily import DailyReport, run_daily
from core.market_state.thresholds import publish_target_model_version
from core.model_validation_evaluation.validation.replay import ModuleConfigs
from core.outcome_tracking.config import publish_outcome_snapshot
from core.scoring.config import publish_scoring_configuration
from core.scoring.engine import Lineage
from infra.db.connection import create_db_engine
from infra.db.schema.identity import universe_version
from infra.deploy.cli import refuse_arguments
from infra.deploy.config import DeploymentProfile, profile_for
from infra.observability.logging import configure_logging, get_logger

__all__ = [
    "UNIVERSE_VERSION_ENV_VAR",
    "ScannerNotReady",
    "build_lineage",
    "main",
    "resolve_universe_version",
    "run_scheduled_scan",
]

_log = get_logger("argus.deploy.scanner")

#: Names the universe version this deployment scans against. Set after a
#: Module 06 universe build, deliberately by a person. See the module
#: docstring on why this is not inferred.
UNIVERSE_VERSION_ENV_VAR = "ARGUS_UNIVERSE_VERSION"


class ScannerNotReady(RuntimeError):
    """The scanner cannot run because a prerequisite has not happened yet.

    Distinct from a scan that ran and failed. Module 18 has a whole
    taxonomy for the second — `FAILED`, `DATA_NOT_READY` — and none of it
    applies to "no universe has ever been built", which is not a scan
    outcome at all. Raising instead of recording a failed run keeps
    Module 18's status table meaning what it says.
    """


@dataclass(frozen=True, slots=True)
class ScannerSettings:
    """What the scanner process reads from its environment."""

    universe_version: str | None = None
    benchmark_ticker: str | None = None

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> ScannerSettings:
        source = env if env is not None else dict(os.environ)
        return cls(
            universe_version=source.get(UNIVERSE_VERSION_ENV_VAR) or None,
            benchmark_ticker=source.get("ARGUS_BENCHMARK_TICKER") or None,
        )


def resolve_universe_version(connection: Connection, requested: str | None) -> UUID:
    """The universe version this scan runs against, named rather than guessed.

    Accepts either the version's UUID or its `version_label` — Module
    06's labels are readable and deterministic
    (`universe-2026-08-28-a1b2c3d4e5f6`), and an operator setting this
    variable will have the label in front of them, not the id.

    Refuses rather than falling back to "the most recent". See the module
    docstring: that fallback is a fifth instance of the ordering hazard
    Module 23 found four of, and this module was told not to add one.
    """
    if not requested:
        raise ScannerNotReady(
            f"{UNIVERSE_VERSION_ENV_VAR} is not set. The scanner records the universe "
            "version every signal was produced under, and this process cannot choose "
            "one for you: 'the newest' is decided by a timestamp that defaults to "
            "transaction-start time, so two versions published together sort "
            "arbitrarily. Set it to the label or id of the version this deployment "
            f"should scan. Available: {_available(connection)}"
        )

    row = connection.execute(
        select(universe_version.c.id, universe_version.c.version_label).where(
            universe_version.c.version_label == requested
        )
    ).one_or_none()
    if row is not None:
        return row.id

    try:
        candidate = UUID(requested)
    except ValueError:
        candidate = None

    if candidate is not None:
        found = connection.execute(
            select(universe_version.c.id).where(universe_version.c.id == candidate)
        ).scalar_one_or_none()
        if found is not None:
            return found

    raise ScannerNotReady(
        f"{UNIVERSE_VERSION_ENV_VAR} is {requested!r}, which matches no universe "
        f"version by label or id. Available: {_available(connection)}"
    )


def build_lineage(
    connection: Connection,
    *,
    universe_version_id: UUID,
    modules: ModuleConfigs | None = None,
    as_of: datetime | None = None,
) -> Lineage:
    """The six configuration versions this scan will record against itself.

    Four published from this build's own configuration, one snapshot
    published for this run, and the universe version resolved separately
    because it is a fact about data rather than about code.

    The four configuration publishers are idempotent by content checksum,
    so a scanner that starts twice under unchanged configuration records
    the same lineage both times — which is what makes a re-run comparable
    to the run it repeats, and a cron schedule that fires twice harmless.

    `publish_outcome_snapshot` needs `as_of` quantised for that to hold.
    See `_snapshot_instant`.
    """
    configs = modules or ModuleConfigs()
    moment = _snapshot_instant(as_of or datetime.now(UTC))

    return Lineage(
        target_model_version_id=publish_target_model_version(
            connection, configs.market_state, description="deployed scanner"
        ),
        feature_schema_version_id=publish_feature_schema_version(
            connection, configs.features, description="deployed scanner"
        ),
        scoring_configuration_id=publish_scoring_configuration(
            connection, configs.scoring, description="deployed scanner"
        ),
        detection_configuration_id=publish_detection_configuration(
            connection, configs.detection, description="deployed scanner"
        ),
        universe_version_id=universe_version_id,
        data_snapshot_id=publish_outcome_snapshot(
            connection, configs.outcome, as_of=moment, description="deployed scanner"
        ),
    )


def run_scheduled_scan(
    engine: Engine,
    *,
    settings: ScannerSettings | None = None,
    profile: DeploymentProfile | None = None,
    now: datetime | None = None,
) -> DailyReport:
    """One scheduled wake-up. Returns Module 18's own report, unchanged.

    The lineage is resolved and published in its own short transaction,
    which then commits, before `run_daily` opens the connections it
    manages itself. Holding one transaction across the whole scan would
    mean a scan of hundreds of securities inside a single long-running
    transaction — and, worse, would defeat Module 18's per-attempt
    connection factory, which exists so a retry is genuinely a retry.
    """
    resolved = settings or ScannerSettings.from_environment()
    (profile or profile_for()).validate()

    with engine.begin() as connection:
        universe_id = resolve_universe_version(connection, resolved.universe_version)
        lineage = build_lineage(connection, universe_version_id=universe_id)

    _log.info(
        "scheduled scan starting",
        extra={
            "event": "scheduled_scan_starting",
            "universe_version_id": str(universe_id),
            "data_snapshot_id": str(lineage.data_snapshot_id),
        },
    )

    report = run_daily(engine.begin, lineage=lineage, now=now)

    _log.info(
        "scheduled scan finished",
        extra={
            "event": "scheduled_scan_finished",
            "scanned": report.scanned,
            "healthy": report.healthy,
        },
    )
    return report


def main(argv: list[str] | None = None) -> int:
    """The cron entrypoint. Exit code is the deployment-visible signal.

    `0` for a healthy wake-up, `1` for an unhealthy one, `2` for a
    prerequisite that has not happened. Three codes rather than two
    because a platform's alerting should be able to tell "the scanner ran
    and something is wrong" from "the scanner could not start" — Module
    18 drew exactly that distinction between a `FAILED` run and a
    `DATA_NOT_READY` one, and collapsing it here would throw it away at
    the last step.
    """
    refuse_arguments("infra.deploy.scanner", argv)
    configure_logging()
    profile = profile_for()

    try:
        engine = create_db_engine(pool_pre_ping=True, pool_size=5, max_overflow=0)
        report = run_scheduled_scan(engine, profile=profile)
    except ScannerNotReady as not_ready:
        _log.error(
            "scanner prerequisite missing",
            extra={"event": "scanner_not_ready", "detail": str(not_ready)},
        )
        return 2

    return 0 if report.healthy else 1


def _available(connection: Connection) -> str:
    """The labels an operator could pick from, for the error message.

    Ordered by label rather than by a timestamp — the labels embed their
    own as-of date, so this sorts chronologically without asking a
    `now()`-defaulted column to break ties it cannot break.
    """
    labels = (
        connection.execute(
            select(universe_version.c.version_label).order_by(universe_version.c.version_label)
        )
        .scalars()
        .all()
    )
    if not labels:
        return "none — no universe has been built yet (Module 06)"
    return ", ".join(labels[-5:])


def _snapshot_instant(moment: datetime) -> datetime:
    """The `as_of` a scan's outcome snapshot is published under, to the day.

    Module 16's `publish_outcome_snapshot` is idempotent by a checksum
    computed over `as_of.isoformat()` — microsecond precision — while the
    `version_label` it inserts is `…@<date>`, which is unique per day.
    The two granularities disagree, so two calls on the same date with
    different microseconds miss the checksum lookup and then collide on
    `uq_data_snapshot_version_label`. A scanner container that restarts
    would crash on its second start.

    That is a defect in Module 16 rather than here, and Module 25's
    boundary is not to change Modules 03-24. So this passes a value that
    is stable within a date, which is the granularity Module 16's own
    label already assumes.

    Midnight UTC rather than the current instant, and that is a real
    choice about the cutoff rather than only a way to make the number
    stable: `as_of_time` is the point-in-time boundary an outcome is
    recomputed against, and Module 18 scans *completed* sessions. A
    cutoff at the start of the current UTC day excludes a day that is not
    finished, which is the conservative direction — the same direction
    every other PIT decision in this project leans.
    """
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


if __name__ == "__main__":  # pragma: no cover - the container's entrypoint
    raise SystemExit(main(sys.argv[1:]))
