"""Alembic migration environment for ARGUS.

The database URL is assembled here from `packages.config` and the
`SecretsProvider` rather than read from `alembic.ini`, so no credential is
ever committed. An explicit `ARGUS_MIGRATION_DATABASE_URL` environment
variable overrides that — used by the test suite, which points migrations
at a throwaway database.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Importing the schema package registers every table on the shared
# metadata object, which is what autogenerate diffs against.
from infra.db.schema import metadata as target_metadata
from infra.observability.logging import logging_is_configured

config = context.config

if config.config_file_name is not None and not logging_is_configured():
    # Two hazards live in this one call, and both were found the hard way.
    #
    # **One: it disables every existing logger.** `fileConfig` defaults to
    # `disable_existing_loggers=True`, which sets `disabled = True` on
    # every logger created before it runs — that is, every application
    # logger, since they are created at import. A process that ran a
    # migration lost the rest of its logging permanently and without a
    # word. Module 22 found it when the WARNING that says authentication
    # is being bypassed vanished under test, and fixed it with the
    # keyword below.
    #
    # **Two: it replaces the root handlers and level.** That survived
    # Module 22's fix. `disable_existing_loggers=False` keeps loggers
    # alive, but `fileConfig` still installs alembic.ini's `[logger_root]`
    # — dropping the level to WARNING and swapping whatever handler the
    # application installed for a plain stderr one. A deployment that
    # configured JSON logging and then migrated would keep its loggers and
    # lose its formatter, its level, and anything shipping records
    # onward. The symptom is worse than the first hazard, because logging
    # still appears to work.
    #
    # So the call is skipped entirely once an application has said it owns
    # logging configuration. A standalone `alembic upgrade head` never
    # calls `configure_logging`, so it still gets alembic.ini's console
    # output exactly as before; an in-process migration leaves the
    # application's logging alone.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

#: Escape hatch for tests and one-off maintenance against a specific database.
URL_OVERRIDE_ENV_VAR = "ARGUS_MIGRATION_DATABASE_URL"


def _database_url() -> str:
    override = os.environ.get(URL_OVERRIDE_ENV_VAR)
    if override:
        return override

    # Imported lazily so that setting the override does not require a
    # loadable application config (e.g. in CI, where no database is set up).
    from infra.db.connection import build_database_url

    return build_database_url().render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
