"""Alert dispatch as a deployable process. The one place the bot token lives.

Modelled on `infra/deploy/scanner.py` and `infra/deploy/ingestion.py` —
same `refuse_arguments`, same `profile_for().validate()`, same structured
start and finish records, same three exit codes.

## Why this is a cron and not part of the web service

The webhook service answers `/start` and `/stop` in milliseconds and is
measured by latency. This wakes once a day, walks every subscriber
against every transition, and is measured by whether it finished. Putting
them in one process would also put the bot token in the public-facing
one, which is the thing `services/telegram/app.py` went out of its way to
avoid.

## The schedule, and the margin

`0 23 * * 1-5` — 23:00 UTC on weekdays.

The three weekday crons now run in a chain, and each margin is sized for
the slowest plausible run of the step before it:

    21:00  ingestion   full-universe OHLCV      ~14-33 min
    22:30  scanner     classify, record states  the transitions this reads
    23:00  dispatch    send the alerts

Thirty minutes after the scanner. The scanner's own budget is four
attempts with exponential backoff capped at a minute, plus the scan
itself, so thirty minutes covers a bad evening rather than only a good
one. It is also deliberately *not* longer: the whole value of an alert is
that it arrives while the information is fresh, and 23:00 UTC is early
evening in the Americas and pre-market in Asia.

Being wrong about the margin is survivable in a way being wrong about
ingestion's is not. A dispatch that runs before the scan finished finds
no transitions, sends nothing, and exits 0 — and the *next* evening's run
still will not send yesterday's alerts, because it asks about the session
`scan_date_for(now)` names. That is a deliberate choice, not an
oversight: a stale alert is worse than a missing one. See
`services/telegram/dispatch.py`.

## Exit codes

`0` healthy — including a run that found nothing to send, which is the
ordinary weekday outcome. `1` when at least one send failed for a reason
that was not "this subscriber blocked the bot", because a blocked
subscriber is a subscriber who left and is handled, while a transient
failure is this job not doing its work. `2` for a prerequisite that has
not happened — here, the bot token being unresolvable.
"""

from __future__ import annotations

import sys
from datetime import datetime

from sqlalchemy import Engine

from infra.db.connection import create_db_engine
from infra.deploy.cli import refuse_arguments
from infra.deploy.config import DeploymentProfile, profile_for
from infra.observability.logging import configure_logging, get_logger
from packages.config import SecretNotFoundError, SecretsProvider
from services.telegram.client import TelegramClient
from services.telegram.config import TelegramConfig
from services.telegram.dispatch import DispatchReport, run_dispatch

__all__ = ["DispatchNotReady", "main", "run_scheduled_dispatch"]

_log = get_logger("argus.deploy.telegram_dispatch")


class DispatchNotReady(RuntimeError):
    """A prerequisite is missing — distinct from a run that failed.

    Same distinction `ScannerNotReady` draws and for the same reason: a
    platform's alerting should be able to tell "the job ran and something
    went wrong" from "the job could not start".
    """


def run_scheduled_dispatch(
    engine: Engine,
    *,
    config: TelegramConfig | None = None,
    profile: DeploymentProfile | None = None,
    secrets: SecretsProvider | None = None,
    now: datetime | None = None,
) -> DispatchReport:
    """One scheduled wake-up. Returns the run's own report, unchanged."""
    settings = config or TelegramConfig()
    (profile or profile_for()).validate()

    try:
        client = TelegramClient(
            secrets=secrets,
            timeout=settings.settings.timeout,
            seconds_between_sends=settings.settings.seconds_between_sends,
        )
    except SecretNotFoundError as missing:
        raise DispatchNotReady(
            "The Telegram bot token is not resolvable, so no message can be sent. "
            "Set TELEGRAM_BOT_TOKEN on this service; it is read through "
            "SecretsProvider and is never a settings default."
        ) from missing

    _log.info("scheduled dispatch starting", extra={"event": "scheduled_dispatch_starting"})
    with client:
        report = run_dispatch(engine, client, config=settings, now=now)

    _log.info(
        "scheduled dispatch finished",
        extra={"event": "scheduled_dispatch_finished", **report.as_dict()},
    )
    return report


def main(argv: list[str] | None = None) -> int:
    """The cron entrypoint. Exit code is the deployment-visible signal."""
    refuse_arguments("infra.deploy.telegram_dispatch", argv)
    configure_logging()
    profile = profile_for()

    try:
        engine = create_db_engine(pool_pre_ping=True, pool_size=2, max_overflow=0)
        report = run_scheduled_dispatch(engine, profile=profile)
    except DispatchNotReady as not_ready:
        _log.error(
            "dispatch prerequisite missing",
            extra={"event": "dispatch_not_ready", "detail": str(not_ready)},
        )
        return 2

    return 0 if report.healthy else 1


if __name__ == "__main__":  # pragma: no cover - the container's entrypoint
    raise SystemExit(main(sys.argv[1:]))
