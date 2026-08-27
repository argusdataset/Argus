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

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` is not a style preference. The
    # default is True, which sets `disabled = True` on every logger that
    # already exists — including every application logger created when
    # the modules were imported. Running a migration in-process therefore
    # silences the rest of the application's logging for the life of that
    # process, permanently and without a word.
    #
    # Module 22 found this the hard way: `argus.identity.seam` logs a
    # WARNING on every request served through the authentication bypass,
    # and that warning vanished in any process that had run Alembic. A
    # deployment that migrates on start-up would have lost exactly the
    # log line that says authentication is being bypassed.
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
