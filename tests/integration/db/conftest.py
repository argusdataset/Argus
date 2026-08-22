"""Fixtures for database integration tests.

These tests run against a real PostgreSQL instance — the guarantees under
test (trigger-enforced immutability, native enum types, NOT NULL on the
PIT columns) are database behaviour, and a SQLite or mock stand-in would
prove nothing about them.

The admin URL comes from `ARGUS_TEST_DATABASE_URL`, defaulting to a local
instance. If no server is reachable the whole module skips with a clear
message rather than failing, so a checkout without Postgres still gets a
green run; CI provides a Postgres service so they genuinely execute.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ALEMBIC_INI = os.path.join(REPO_ROOT, "infra", "db", "alembic.ini")

DEFAULT_ADMIN_URL = "postgresql+psycopg://argus:argus_local_dev@127.0.0.1:5432/postgres"


def _admin_url() -> URL:
    return make_url(os.environ.get("ARGUS_TEST_DATABASE_URL", DEFAULT_ADMIN_URL))


@pytest.fixture(scope="session")
def admin_url() -> URL:
    """URL of a reachable server, or skip the whole database test suite."""
    url = _admin_url()
    try:
        engine = create_engine(url, isolation_level="AUTOCOMMIT")
        with engine.connect():
            pass
        engine.dispose()
    except sqlalchemy.exc.OperationalError as exc:  # pragma: no cover - env dependent
        pytest.skip(f"No PostgreSQL reachable at {url.render_as_string()}: {exc}")
    return url


@pytest.fixture(scope="session")
def migrated_database(admin_url: URL) -> Iterator[URL]:
    """Create a throwaway database, migrate it to head, drop it afterwards.

    Session-scoped: running the full migration once and sharing it keeps
    the suite fast, and every test that mutates data cleans up after
    itself or writes only to tables it owns.
    """
    db_name = f"argus_test_{uuid.uuid4().hex[:12]}"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))

    test_url = admin_url.set(database=db_name)
    previous = os.environ.get("ARGUS_MIGRATION_DATABASE_URL")
    os.environ["ARGUS_MIGRATION_DATABASE_URL"] = test_url.render_as_string(hide_password=False)
    try:
        command.upgrade(Config(ALEMBIC_INI), "head")
        yield test_url
    finally:
        if previous is None:
            os.environ.pop("ARGUS_MIGRATION_DATABASE_URL", None)
        else:
            os.environ["ARGUS_MIGRATION_DATABASE_URL"] = previous
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin_engine.dispose()


@pytest.fixture(scope="session")
def engine(migrated_database: URL) -> Iterator[Engine]:
    """Engine bound to the migrated throwaway database."""
    eng = create_engine(migrated_database)
    yield eng
    eng.dispose()
