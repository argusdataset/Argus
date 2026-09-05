"""Module 29's daily ownership signals as a deployable process.

Modelled on `infra/deploy/news_signals.py`, which is itself modelled on
`infra/deploy/ingestion.py` and `infra/deploy/scanner.py`: same
`refuse_arguments`, same `profile_for().validate()`, same structured
start and finish logging, same three exit codes. The universe version is
resolved with `scanner.resolve_universe_version` — the same function, not
a copy — so no two ARGUS processes can end up pointed at different
universes.

## A second cron rather than more work in the first

`news_signals` and this one could have been one process. They are not,
because they fail for different reasons and a shared exit code would
hide which happened: the news signals read `canonical_news`, these read
`insider_trades` and `institutional_ownership`, and those three tables
are filled by different halves of Module 26's ingestion. A run that
exits 1 should name one thing that is wrong, not two things it might be.

## No FMP client, no secret beyond the database

Both signals read tables Module 26's ingestion already filled, so nothing
here resolves a provider credential. The ordering hazard that used to
crash processes on a bare `DATABASE_URL` (G3) is fixed at the root now
anyway — `AppConfig` derives its database group from the connection
string — but this process would not have hit it regardless.

## Exit codes

`0` healthy — including a run with nothing due, the ordinary weekend
outcome. `1` ran but stored nothing for a non-empty universe. `2` a
prerequisite has not happened: no universe version is resolvable, the
same condition that stops the scanner, ingestion and the news signals.
"""

from __future__ import annotations

import sys

from sqlalchemy import Engine

from core.ownership_signals.config import OwnershipSignalConfig
from core.ownership_signals.orchestrator import (
    OwnershipSignalRunReport,
    run_daily_ownership_signals,
)
from infra.db.connection import create_db_engine
from infra.deploy.cli import refuse_arguments
from infra.deploy.config import DeploymentProfile, profile_for
from infra.deploy.scanner import ScannerNotReady, ScannerSettings, resolve_universe_version
from infra.observability.logging import configure_logging, get_logger

__all__ = ["main", "run_scheduled_ownership_signals"]

_log = get_logger("argus.deploy.ownership_signals")


def run_scheduled_ownership_signals(
    engine: Engine,
    *,
    settings: ScannerSettings | None = None,
    config: OwnershipSignalConfig | None = None,
    profile: DeploymentProfile | None = None,
) -> OwnershipSignalRunReport:
    """One scheduled wake-up. Returns the run's own report, unchanged."""
    resolved = settings or ScannerSettings.from_environment()
    (profile or profile_for()).validate()

    with engine.begin() as connection:
        universe_id = resolve_universe_version(connection, resolved.universe_version)

    _log.info(
        "scheduled ownership signal run starting",
        extra={
            "event": "scheduled_ownership_signals_starting",
            "universe_version_id": str(universe_id),
        },
    )

    report = run_daily_ownership_signals(engine, universe_version_id=universe_id, config=config)

    _log.info(
        "scheduled ownership signal run finished",
        extra={"event": "scheduled_ownership_signals_finished", **report.as_dict()},
    )
    return report


def main(argv: list[str] | None = None) -> int:
    """The cron entrypoint. Exit code is the deployment-visible signal."""
    refuse_arguments("infra.deploy.ownership_signals", argv)
    configure_logging()
    profile = profile_for()

    try:
        engine = create_db_engine(pool_pre_ping=True, pool_size=2, max_overflow=0)
        report = run_scheduled_ownership_signals(engine, profile=profile)
    except ScannerNotReady as not_ready:
        _log.error(
            "ownership signal prerequisite missing",
            extra={"event": "ownership_signals_not_ready", "detail": str(not_ready)},
        )
        return 2

    return 0 if report.healthy else 1


if __name__ == "__main__":  # pragma: no cover - the container's entrypoint
    raise SystemExit(main(sys.argv[1:]))
