"""Daily ingestion as a deployed process.

Mostly the same shape as `test_scanner_process.py`, and deliberately so:
the two crons read the same variable through the same function, and the
tests that matter here are the ones proving they cannot drift apart.

The one thing this file does *not* do is make an HTTP request. Whether
the fetchers work is Module 04's business and Module 04 tests it against
recorded payloads; what is under test here is the process wrapper — what
it refuses, what it resolves, and what it exits with.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from infra.deploy import ingestion, scanner
from infra.deploy.cli import UnexpectedArguments
from infra.deploy.migrate import upgrade_to_head
from infra.deploy.processes import PROCESSES
from infra.deploy.scanner import UNIVERSE_VERSION_ENV_VAR, ScannerNotReady


@pytest.fixture
def migrated(fresh_engine: Engine, alembic_target) -> Engine:
    upgrade_to_head(fresh_engine)
    return fresh_engine


def test_it_resolves_the_universe_with_the_scanners_own_function():
    """Not a copy. The same object, asserted by identity.

    Two implementations of "which universe version" could be pointed at
    different ones — bars ingested for one universe's members, coverage
    measured against another's — and every symptom of that would look
    like a provider delay.
    """
    assert ingestion.resolve_universe_version is scanner.resolve_universe_version
    assert ingestion.ScannerSettings is scanner.ScannerSettings


def test_it_refuses_to_run_when_no_universe_version_is_named(migrated: Engine):
    """Same refusal, same variable, same reason as the scanner's."""
    settings = scanner.ScannerSettings(universe_version=None)

    with pytest.raises(ScannerNotReady, match=UNIVERSE_VERSION_ENV_VAR):
        import asyncio

        asyncio.run(ingestion.run_scheduled_ingestion(migrated, settings=settings))


def test_a_missing_universe_exits_with_the_prerequisite_code(migrated: Engine, monkeypatch):
    """Exit 2, not 1. "Could not start" and "ran and failed" need different responses.

    Collapsing them would mean an operator woken for a missing
    environment variable in the same way as for a broken feed.
    """
    monkeypatch.delenv(UNIVERSE_VERSION_ENV_VAR, raising=False)
    monkeypatch.setattr(ingestion, "create_db_engine", lambda **kwargs: migrated)

    assert ingestion.main([]) == 2


def test_the_entrypoint_refuses_arguments_it_cannot_use():
    """The `&&` failure that already happened once, on a different service.

    A start command is a string in a web form. An entrypoint that
    silently discards its argv cannot tell anybody it was invoked wrongly.
    """
    with pytest.raises(UnexpectedArguments):
        ingestion.main(["&&", "uvicorn", "infra.deploy.asgi:terminal_app"])


def test_the_process_definition_and_the_module_agree():
    """The command string is the only link between the platform and the code."""
    assert PROCESSES["ingestion"].command() == "python -m infra.deploy.ingestion"
    assert callable(ingestion.main)


def test_it_reads_the_same_environment_variable_the_scanner_does(monkeypatch):
    monkeypatch.setenv(UNIVERSE_VERSION_ENV_VAR, "universe-2026-03-09-abcdef123456")

    settings = scanner.ScannerSettings.from_environment()

    assert settings.universe_version == "universe-2026-03-09-abcdef123456"


def _label(engine: Engine, label: str) -> str:
    with engine.begin() as connection:
        return str(
            connection.execute(
                text(
                    "INSERT INTO universe_version (version_label, definition, as_of_date) "
                    "VALUES (:label, '{}'::jsonb, now()) RETURNING id"
                ),
                {"label": label},
            ).scalar_one()
        )


def test_a_named_version_resolves_the_same_way_for_both_processes(migrated: Engine):
    """The property the shared function buys, asserted rather than assumed."""
    identifier = _label(migrated, "universe-2026-03-09-module26")

    with migrated.connect() as connection:
        from_ingestion = ingestion.resolve_universe_version(
            connection, "universe-2026-03-09-module26"
        )
        from_scanner = scanner.resolve_universe_version(connection, "universe-2026-03-09-module26")

    assert str(from_ingestion) == identifier
    assert from_ingestion == from_scanner
