"""Module 28's daily news-volume signal as a deployed process.

Mostly the same shape as `test_ingestion_process.py`: the universe
version is resolved through the same function the scanner and ingestion
already use, and what is under test here is the process wrapper — what it
refuses, what it resolves, and what it exits with — not the assessment
logic itself, which is unit- and integration-tested in
`tests/unit/news_signals/` and `tests/integration/news_signals/`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from infra.deploy import news_signals, scanner
from infra.deploy.cli import UnexpectedArguments
from infra.deploy.migrate import upgrade_to_head
from infra.deploy.processes import PROCESSES
from infra.deploy.scanner import UNIVERSE_VERSION_ENV_VAR, ScannerNotReady


@pytest.fixture
def migrated(fresh_engine: Engine, alembic_target) -> Engine:
    upgrade_to_head(fresh_engine)
    return fresh_engine


def test_it_resolves_the_universe_with_the_scanners_own_function():
    """Not a copy. The same object, asserted by identity — the reason
    Module 26's own equivalent test exists: two independent "which
    universe" implementations could disagree, and every symptom of that
    would look like something else entirely."""
    assert news_signals.resolve_universe_version is scanner.resolve_universe_version
    assert news_signals.ScannerSettings is scanner.ScannerSettings


def test_it_refuses_to_run_when_no_universe_version_is_named(migrated: Engine):
    """Same refusal, same variable, same reason as the scanner's."""
    settings = scanner.ScannerSettings(universe_version=None)

    with pytest.raises(ScannerNotReady, match=UNIVERSE_VERSION_ENV_VAR):
        news_signals.run_scheduled_news_signals(migrated, settings=settings)


def test_a_missing_universe_exits_with_the_prerequisite_code(migrated: Engine, monkeypatch):
    """Exit 2, not 1 — "could not start" and "ran and failed" need
    different responses from whoever is alerted."""
    monkeypatch.delenv(UNIVERSE_VERSION_ENV_VAR, raising=False)
    monkeypatch.setattr(news_signals, "create_db_engine", lambda **kwargs: migrated)

    assert news_signals.main([]) == 2


def test_the_entrypoint_refuses_arguments_it_cannot_use():
    """The `&&` failure that already happened once, on a different service."""
    with pytest.raises(UnexpectedArguments):
        news_signals.main(["&&", "uvicorn", "infra.deploy.asgi:terminal_app"])


def test_the_process_definition_and_the_module_agree():
    """The command string is the only link between the platform and the code."""
    assert PROCESSES["news_signals"].command() == "python -m infra.deploy.news_signals"
    assert callable(news_signals.main)


def test_a_named_universe_version_produces_a_healthy_run(migrated: Engine):
    """A resolvable universe (even an empty one) is enough to exit 0 —
    Module 28's own report is healthy whenever nothing due failed to be
    assessed, which an empty universe trivially satisfies."""
    identifier = _label(migrated, "universe-2026-03-09-module28")
    settings = scanner.ScannerSettings(universe_version=identifier)

    report = news_signals.run_scheduled_news_signals(migrated, settings=settings)

    assert report.healthy is True


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


def test_a_named_version_resolves_the_same_way_as_the_other_two_crons(migrated: Engine):
    """The property the shared function buys, asserted rather than assumed."""
    identifier = _label(migrated, "universe-2026-03-09-module28b")

    with migrated.connect() as connection:
        from_news_signals = news_signals.resolve_universe_version(
            connection, "universe-2026-03-09-module28b"
        )
        from_scanner = scanner.resolve_universe_version(connection, "universe-2026-03-09-module28b")

    assert str(from_news_signals) == identifier
    assert from_news_signals == from_scanner
