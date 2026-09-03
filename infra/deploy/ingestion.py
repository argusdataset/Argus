"""Daily ingestion as a deployable process. The job the scanner assumes exists.

## Why this is the most consequential cron in the project

Module 18's scanner has been deployable since Module 25 and, until this
process exists, could never have done anything. `readiness.py` counts how
many universe members hold a bar for the scan date and refuses to scan a
day that is short — and nothing in production was writing to
`canonical_ohlcv`. Module 04's fetchers had zero non-test callers;
Module 05's `normalize_security` and `persist` likewise. The scanner
would have woken every weekday, found no data, recorded `DATA_NOT_READY`,
and done that forever.

So this is not an enhancement to the deployment. It is the half of it
that was missing.

## Modelled on `scanner.py`, including what it refuses to guess

Same shape and for the same reasons: `refuse_arguments` because a start
command is a string in a web form, `profile_for().validate()` because a
process that boots with the wrong profile is worse than one that does
not boot, structured start and finish logging, and meaningful exit codes.

`universe_version_id` is resolved with `scanner.resolve_universe_version`
— the same function, not a copy. It refuses to fall back to "the most
recent version", which is the ordering hazard Module 23 catalogued four
instances of, and it reads the same `ARGUS_UNIVERSE_VERSION` variable the
scanner does. Reusing it means the two processes cannot end up pointed at
different universes, which would be a silent and very confusing failure:
bars ingested for one universe's members, coverage measured against
another's.

## The schedule

`0 21 * * 1-5` — 21:00 UTC on weekdays, ninety minutes before the
scanner's 22:30. The margin is sized for the slow path: at FMP's Starter
limit of 300 requests a minute a ten-thousand-symbol universe takes about
thirty-three minutes, and at Premium's 750 about fourteen. Either
finishes well inside the window, and the deep-refresh half — which has no
deadline, because nothing checks how fresh a fundamental is — is
deliberately last so it cannot spend the price pull's margin.

## Exit codes

`0` healthy, `1` ran but the session it ingested is not scannable, `2` a
prerequisite has not happened. The same three the scanner uses and the
same distinction: "the job ran and something is wrong" and "the job could
not start" need different responses from whoever is alerted.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from uuid import UUID

from sqlalchemy import Engine

from core.ingestion.config import IngestionConfig
from core.ingestion.orchestrator import IngestionReport, run_daily_ingestion
from data.provider_adapters.fmp.client import FmpClient
from data.provider_adapters.fmp.fetchers import FmpFetcher
from infra.db.connection import create_db_engine
from infra.deploy.cli import refuse_arguments
from infra.deploy.config import DeploymentProfile, profile_for
from infra.deploy.scanner import ScannerNotReady, ScannerSettings, resolve_universe_version
from infra.observability.logging import configure_logging, get_logger

__all__ = ["main", "run_scheduled_ingestion"]

_log = get_logger("argus.deploy.ingestion")


async def run_scheduled_ingestion(
    engine: Engine,
    *,
    settings: ScannerSettings | None = None,
    config: IngestionConfig | None = None,
    profile: DeploymentProfile | None = None,
    now: datetime | None = None,
) -> IngestionReport:
    """One scheduled wake-up. Returns the run's own report, unchanged.

    The universe version is resolved and its transaction committed before
    any fetching starts. Holding one transaction across a run that makes
    ten thousand HTTP requests would keep a connection open for half an
    hour doing nothing, and would defeat the per-security transaction
    boundaries the ingestion path chose on purpose.
    """
    resolved = settings or ScannerSettings.from_environment()
    (profile or profile_for()).validate()

    with engine.begin() as connection:
        universe_id: UUID = resolve_universe_version(connection, resolved.universe_version)

    _log.info(
        "scheduled ingestion starting",
        extra={
            "event": "scheduled_ingestion_starting",
            "universe_version_id": str(universe_id),
        },
    )

    # One client for the whole run, so both phases draw on one set of
    # token buckets rather than two independent budgets against a
    # per-minute ceiling FMP enforces once.
    async with FmpClient() as client:
        report = await run_daily_ingestion(
            engine,
            FmpFetcher(client),
            universe_version_id=universe_id,
            config=config,
            now=now,
        )

    _log.info(
        "scheduled ingestion finished",
        extra={
            "event": "scheduled_ingestion_finished",
            "healthy": report.healthy,
            **report.as_dict(),
        },
    )
    return report


def main(argv: list[str] | None = None) -> int:
    """The cron entrypoint. Exit code is the deployment-visible signal."""
    refuse_arguments("infra.deploy.ingestion", argv)
    configure_logging()
    profile = profile_for()

    try:
        engine = create_db_engine(pool_pre_ping=True, pool_size=5, max_overflow=0)
        report = asyncio.run(run_scheduled_ingestion(engine, profile=profile))
    except ScannerNotReady as not_ready:
        _log.error(
            "ingestion prerequisite missing",
            extra={"event": "ingestion_not_ready", "detail": str(not_ready)},
        )
        return 2

    return 0 if report.healthy else 1


if __name__ == "__main__":  # pragma: no cover - the container's entrypoint
    raise SystemExit(main(sys.argv[1:]))
