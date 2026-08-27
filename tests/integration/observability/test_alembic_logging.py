"""The standing regression test for the Alembic logging incident.

## What happened

`fileConfig()` defaults to `disable_existing_loggers=True`, which sets
`disabled = True` on every logger created before it runs. Application
loggers are created at import, so any process that ran a migration lost
the rest of its logging permanently and silently. Module 22 found it
because a `caplog` assertion failed while the code under test was
correct.

## Why a regression test needs a real migration

The obvious test — assert the keyword appears in `env.py` — passes
against a file that has the keyword and a second `fileConfig` call
somewhere else, and against a new migration-invoking path that nobody
thought to check. It tests the fix, not the property.

So the test here runs an actual `command.upgrade` in-process, in the same
interpreter as a configured application logger, and asserts the logger
still emits afterwards. That is the scenario that produced the bug, and
it would fail for any cause — this `fileConfig` call, a different one, a
library doing the same thing, or a future path that calls
`logging.disable`.

## And the half the original fix did not close

`disable_existing_loggers=False` keeps loggers alive. It does not stop
`fileConfig` from **replacing the root handlers and resetting the root
level**, which alembic.ini's `[logger_root]` tells it to do. An
application with a JSON handler and DEBUG level would keep every logger
and lose its formatter, its level, and anything shipping records onward —
a worse symptom than the original, because logging still appears to work.
Module 23 closed that by having `env.py` skip `fileConfig` entirely once
`configure_logging` has claimed ownership, and the tests below cover both
halves.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from alembic import command
from alembic.config import Config

from infra.observability.logging import (
    configure_logging,
    get_logger,
    logging_is_configured,
    reset_logging,
)
from tests.integration.db.conftest import (  # noqa: F401
    ALEMBIC_INI,
    admin_url,
    engine,
    migrated_database,
)


@pytest.fixture(autouse=True)
def clean_logging():
    """Leave global logging exactly as it was found.

    These tests mutate process-wide state, which is the only way to
    exercise the defect — it *is* process-wide state corruption.
    """
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    reset_logging()
    root.handlers[:] = before
    root.setLevel(level)


def _upgrade() -> None:
    """Run migrations in-process, the way a deployment migrating on start does."""
    command.upgrade(Config(ALEMBIC_INI), "head")


# --------------------------------------------------------------------------
# The original defect
# --------------------------------------------------------------------------


def test_a_logger_still_emits_after_a_migration_runs_in_process(migrated_database):  # noqa: F811
    """The regression test proper. This is what would have caught it.

    An application logger, a real in-process migration, then a log call.
    The assertion is on the record arriving, not on how `env.py` is
    written — so it fails for any cause of the same symptom.
    """
    stream = io.StringIO()
    configure_logging(level=logging.INFO, stream=stream)
    log = get_logger("argus.regression.probe")

    _upgrade()

    log.warning("still here")

    assert log.disabled is False, "the migration disabled an application logger"
    assert "still here" in stream.getvalue()


def test_every_module_namespace_survives_a_migration(migrated_database):  # noqa: F811
    """Not just `services/identity/` — the audit Part 0 asked for.

    The original defect was found through one logger because one logger
    existed. This asserts the property for a representative name per
    package, so the next module to add logging inherits the guarantee
    rather than rediscovering the bug.
    """
    stream = io.StringIO()
    configure_logging(level=logging.INFO, stream=stream)

    names = [
        "argus.identity.seam",
        "argus.live_scanner",
        "argus.market_state",
        "argus.explanation",
        "argus.observability",
        "argus.terminal",
        "argus.public_stats",
        "argus.intelligence",
        "argus.data.ingestion",
    ]
    loggers = [get_logger(name) for name in names]

    _upgrade()

    for logger in loggers:
        logger.warning("post-migration", extra={"event": "probe"})

    emitted = stream.getvalue()
    assert [logger.name for logger in loggers if logger.disabled] == []
    for name in names:
        assert name in emitted, f"{name} produced no record after the migration"


def test_the_real_seam_logger_survives_a_migration(migrated_database):  # noqa: F811
    """The exact logger whose silence was the original symptom."""
    from services.identity import seam

    stream = io.StringIO()
    configure_logging(level=logging.INFO, stream=stream)

    _upgrade()

    seam._log.warning(seam.STUB_WARNING, "X-Argus-User", "some-user-id")

    assert seam._log.disabled is False
    assert "AUTHENTICATION BYPASS" in stream.getvalue()


# --------------------------------------------------------------------------
# The half the original fix did not close
# --------------------------------------------------------------------------


def test_a_migration_does_not_replace_the_applications_handler(migrated_database):  # noqa: F811
    """`disable_existing_loggers=False` alone would not have caught this.

    alembic.ini's `[logger_root]` tells `fileConfig` to install a plain
    stderr handler and set WARNING. Applied to a configured application
    that is a silent downgrade: loggers survive, the JSON formatting and
    the log level do not.
    """
    stream = io.StringIO()
    handler = configure_logging(level=logging.DEBUG, stream=stream)
    root = logging.getLogger()

    _upgrade()

    assert handler in root.handlers, "the migration replaced the application's handler"
    assert root.level == logging.DEBUG, "the migration reset the application's log level"

    get_logger("argus.after.migration").debug("json please", extra={"event": "probe"})
    line = stream.getvalue().strip().splitlines()[-1]
    assert json.loads(line)["event"] == "probe", "output is no longer the JSON format"


def test_the_environment_skips_file_config_once_logging_is_owned():
    """The mechanism, checked directly rather than only through its effect."""
    assert logging_is_configured() is False

    configure_logging(stream=io.StringIO())

    assert logging_is_configured() is True

    reset_logging()
    assert logging_is_configured() is False


def test_a_standalone_migration_still_gets_alembic_console_logging(migrated_database):  # noqa: F811
    """The other direction: nothing was taken away from `alembic upgrade head`.

    A deployment running migrations from a shell has no application
    logging to protect, and should still see Alembic's own output. The
    guard is conditional on ownership precisely so this case is unchanged.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    assert logging_is_configured() is False

    _upgrade()

    assert root.handlers, "a standalone migration configured no logging at all"
    assert logging.getLogger("alembic").getEffectiveLevel() <= logging.INFO
