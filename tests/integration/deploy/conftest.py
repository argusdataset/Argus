"""Fixtures for Module 25. Real databases, because that is what is being tested.

Reuses Module 03's `admin_url` so a checkout without PostgreSQL skips
rather than fails, exactly as every other database suite here does.

Two fixtures beyond that: an *unmigrated* database, because the migration
step's whole job is the transition from nothing to head — a fixture that
hands over an already-migrated database cannot test it — and an archive
directory, so a restore drill's dump does not land somewhere a developer
has to find later.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from tests.integration.db.conftest import admin_url  # noqa: F401


@pytest.fixture
def fresh_database(admin_url: URL) -> Iterator[URL]:  # noqa: F811
    """An empty database with no `alembic_version` row at all.

    Dropped afterwards, and the connections to it terminated first: a
    migration leaves a pooled connection open and `DROP DATABASE` refuses
    while one exists.
    """
    name = f"argus_m25_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))

    url = admin_url.set(database=name)
    try:
        yield url
    finally:
        _drop(admin, name)
        admin.dispose()


@pytest.fixture
def fresh_engine(fresh_database: URL) -> Iterator[Engine]:
    engine = create_engine(fresh_database)
    yield engine
    engine.dispose()


@pytest.fixture
def alembic_target(fresh_database: URL) -> Iterator[URL]:
    """Point Alembic's `env.py` at the throwaway database for one test.

    The same variable Module 03's own fixture uses, restored afterwards —
    `command.upgrade` reads the environment rather than taking a URL.
    """
    previous = os.environ.get("ARGUS_MIGRATION_DATABASE_URL")
    os.environ["ARGUS_MIGRATION_DATABASE_URL"] = fresh_database.render_as_string(
        hide_password=False
    )
    try:
        yield fresh_database
    finally:
        if previous is None:
            os.environ.pop("ARGUS_MIGRATION_DATABASE_URL", None)
        else:
            os.environ["ARGUS_MIGRATION_DATABASE_URL"] = previous


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    return tmp_path / "archives"


def _drop(admin: Engine, name: str) -> None:
    with admin.connect() as connection:
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": name},
        )
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


@pytest.fixture
def drop_database(admin_url: URL):  # noqa: F811
    """Clean up a database a test created by name — the restore drill's target."""
    created: list[str] = []

    def _register(name: str) -> str:
        created.append(name)
        return name

    yield _register

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    for name in created:
        _drop(admin, name)
    admin.dispose()


@pytest.fixture
def make_user(deployed: Engine):
    """A real `users` row in the database the composed services read.

    Real rather than a bare UUID, for the reason Module 19's own conftest
    gives: the identity stub resolves the header against `users` and
    refuses an id that names nothing. A test asserting the stub is *off*
    has to supply an id it would otherwise have accepted, or it proves
    nothing.
    """
    role_id = None

    def _make(label: str) -> uuid.UUID:
        nonlocal role_id
        with deployed.begin() as connection:
            if role_id is None:
                role_id = connection.execute(
                    text("INSERT INTO roles (name) VALUES (:name) RETURNING id"),
                    {"name": f"deploy-{uuid.uuid4()}"},
                ).scalar_one()
            return connection.execute(
                text(
                    "INSERT INTO users (email, display_name, role_id) "
                    "VALUES (:email, :name, :role) RETURNING id"
                ),
                {
                    "email": f"{label}-{uuid.uuid4()}@example.test",
                    "name": label,
                    "role": role_id,
                },
            ).scalar_one()

    return _make
