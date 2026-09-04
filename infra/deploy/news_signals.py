"""Module 28's daily news-volume signal as a deployable process.

Modelled on `infra/deploy/ingestion.py` and `infra/deploy/scanner.py`: same
`refuse_arguments`, same `profile_for().validate()`, same structured start
and finish logging, same three exit codes. The universe version is
resolved with `scanner.resolve_universe_version` — the same function, not
a copy — for the identical reason Module 26's ingestion reuses it: two
processes with independent "which universe" logic could end up pointed at
different versions, and every symptom of that would look like something
else.

## No FMP client, no bot token

Unlike ingestion or Telegram dispatch, this process resolves no secret
beyond the database connection: `canonical_news` is already being filled
by Module 26's own deep refresh, and Module 28 only reads it. That also
means this process does not carry `docs/architecture/KNOWN_ISSUES.md` G3's
exposure — nothing here calls `get_config()`, so the ordering hazard that
crashes the ingestion cron on a bare `DATABASE_URL` does not apply (see
`core/news_signals/orchestrator.py`'s own docstring, which states the same
thing at the level this process wraps).

## Exit codes

`0` healthy — including a run with nothing due, the ordinary outcome
outside a scheduled window. `1` ran but stored nothing for a non-empty
universe. `2` a prerequisite has not happened — here, no universe version
is resolvable, the same condition that stops the scanner and ingestion.
"""

from __future__ import annotations

import sys

from sqlalchemy import Engine

from core.news_signals.config import NewsSignalConfig
from core.news_signals.orchestrator import NewsSignalRunReport, run_daily_news_signals
from infra.db.connection import create_db_engine
from infra.deploy.cli import refuse_arguments
from infra.deploy.config import DeploymentProfile, profile_for
from infra.deploy.scanner import ScannerNotReady, ScannerSettings, resolve_universe_version
from infra.observability.logging import configure_logging, get_logger

__all__ = ["main", "run_scheduled_news_signals"]

_log = get_logger("argus.deploy.news_signals")


def run_scheduled_news_signals(
    engine: Engine,
    *,
    settings: ScannerSettings | None = None,
    config: NewsSignalConfig | None = None,
    profile: DeploymentProfile | None = None,
) -> NewsSignalRunReport:
    """One scheduled wake-up. Returns the run's own report, unchanged."""
    resolved = settings or ScannerSettings.from_environment()
    (profile or profile_for()).validate()

    with engine.begin() as connection:
        universe_id = resolve_universe_version(connection, resolved.universe_version)

    _log.info(
        "scheduled news signal run starting",
        extra={
            "event": "scheduled_news_signals_starting",
            "universe_version_id": str(universe_id),
        },
    )

    report = run_daily_news_signals(engine, universe_version_id=universe_id, config=config)

    _log.info(
        "scheduled news signal run finished",
        extra={"event": "scheduled_news_signals_finished", **report.as_dict()},
    )
    return report


def main(argv: list[str] | None = None) -> int:
    """The cron entrypoint. Exit code is the deployment-visible signal."""
    refuse_arguments("infra.deploy.news_signals", argv)
    configure_logging()
    profile = profile_for()

    try:
        engine = create_db_engine(pool_pre_ping=True, pool_size=2, max_overflow=0)
        report = run_scheduled_news_signals(engine, profile=profile)
    except ScannerNotReady as not_ready:
        _log.error(
            "news signal prerequisite missing",
            extra={"event": "news_signals_not_ready", "detail": str(not_ready)},
        )
        return 2

    return 0 if report.healthy else 1


if __name__ == "__main__":  # pragma: no cover - the container's entrypoint
    raise SystemExit(main(sys.argv[1:]))
