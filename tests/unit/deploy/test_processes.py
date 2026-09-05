"""Process definitions. No database — this is what the platform is told to run.

The failure this file exists to catch is quiet: a process whose start
command names a factory that does not exist, or two services that
accidentally run the same one. Neither breaks a test suite; both break a
deployment, and only after it has been deployed.
"""

from __future__ import annotations

import pytest

from infra.deploy import asgi
from infra.deploy.processes import (
    PRE_DEPLOY_COMMAND,
    PROCESSES,
    api_processes,
    start_command,
)

WEB_SERVICES = {"terminal", "public_stats", "intelligence", "identity", "telegram", "health"}
CRON_SERVICES = {
    "ingestion",
    "scanner",
    "telegram_dispatch",
    "retention",
    "news_signals",
    "ownership_signals",
}


def test_every_service_module_19_to_29_built_has_a_process():
    """The five API services, the health service, and all six cron jobs.

    Written as an equality rather than a series of `in` checks so that a
    service added without a process definition, or a definition left
    behind after a service is removed, fails here.
    """
    assert set(PROCESSES) == WEB_SERVICES | CRON_SERVICES


def test_web_and_cron_are_the_only_kinds():
    assert {process.kind for process in PROCESSES.values()} == {"web", "cron"}


def test_each_process_has_a_distinct_start_command():
    """Five web services running the same app would be an invisible failure.

    Every one of them would boot, pass its health check and serve — the
    wrong service, on the wrong domain.
    """
    commands = [process.command() for process in PROCESSES.values()]
    assert len(commands) == len(set(commands))


@pytest.mark.parametrize("name", sorted(WEB_SERVICES))
def test_web_process_names_a_factory_that_exists(name: str):
    """The command string is the only link between config and code.

    Nothing else imports `terminal_app` by that name, so a rename would
    leave a start command pointing at nothing and no other test would
    notice.
    """
    process = PROCESSES[name]
    factory = getattr(asgi, process.asgi_factory or "", None)
    assert callable(factory), f"{process.asgi_factory} is not importable from infra.deploy.asgi"


@pytest.mark.parametrize("name", sorted(WEB_SERVICES))
def test_web_process_serves_uvicorn_on_the_platform_port(name: str):
    command = PROCESSES[name].command()
    assert command.startswith("uvicorn ")
    assert "--factory" in command
    assert "--port ${PORT:-8000}" in command
    assert "--host 0.0.0.0" in command  # noqa: S104 - a container binds all interfaces


@pytest.mark.parametrize("name", sorted(WEB_SERVICES))
def test_web_process_defaults_to_one_worker(name: str):
    """Module 24's rate limiter counts in process memory.

    N workers means N independent ceilings. `config.py` refuses to boot a
    production process with more than one and no shared store; this is
    the other half — the default that keeps the refusal from ever being
    reached by accident.
    """
    assert "--workers ${WEB_CONCURRENCY:-1}" in PROCESSES[name].command()


@pytest.mark.parametrize("name", sorted(WEB_SERVICES))
def test_web_process_disables_the_uvicorn_access_log(name: str):
    """One JSON object per line is a convention every log consumer relies on."""
    assert "--no-access-log" in PROCESSES[name].command()


@pytest.mark.parametrize("name", sorted(WEB_SERVICES))
def test_web_process_is_health_checked(name: str):
    assert PROCESSES[name].health_path == "/health/live"


@pytest.mark.parametrize("name", sorted(CRON_SERVICES))
def test_cron_process_has_a_schedule_and_no_health_check(name: str):
    """A cron service has no port to poll and nothing running between firings."""
    process = PROCESSES[name]
    assert process.schedule
    assert process.health_path is None
    assert process.asgi_factory is None


def test_the_scanner_is_not_an_api_process():
    """The requirement is that these are genuinely different process definitions.

    Same image, different command, different lifecycle — a scan's memory
    footprint does not belong in the request path.
    """
    assert PROCESSES["scanner"] not in api_processes()
    assert PROCESSES["scanner"].command() == "python -m infra.deploy.scanner"


def test_the_scanner_runs_after_the_us_close_on_weekdays():
    minute, hour, _, _, weekday = PROCESSES["scanner"].schedule.split()
    assert (int(hour), int(minute)) == (22, 30)
    assert weekday == "1-5"


def test_ingestion_runs_before_the_scanner_on_the_same_weekdays():
    """The two weekday crons are ordered, and the order is the deadline.

    The scanner refuses to scan a session whose OHLCV coverage is short,
    and ingestion is what delivers it — so a schedule change that moved
    ingestion after the scanner would make every scan `DATA_NOT_READY`
    with nothing anywhere naming the cause. Asserted as an inequality
    between the two definitions rather than as two literal times, so
    moving either one keeps the relationship checked.
    """
    ingestion = PROCESSES["ingestion"].schedule.split()
    scanner = PROCESSES["scanner"].schedule.split()

    assert ingestion[-1] == scanner[-1] == "1-5"
    assert (int(ingestion[1]), int(ingestion[0])) < (int(scanner[1]), int(scanner[0]))


def test_the_ingestion_process_runs_its_own_module():
    """The command string is the only link between config and code."""
    assert PROCESSES["ingestion"].command() == "python -m infra.deploy.ingestion"
    assert PROCESSES["ingestion"] not in api_processes()


def test_retention_runs_every_day_including_weekends():
    """Sessions expire on Saturdays too."""
    assert PROCESSES["retention"].schedule.split()[-1] == "*"


def test_the_pre_deploy_command_runs_the_migration_module():
    assert PRE_DEPLOY_COMMAND == "python -m infra.deploy.migrate"


def test_start_command_reads_from_the_one_definition():
    assert start_command("identity") == PROCESSES["identity"].command()
