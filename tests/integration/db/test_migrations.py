"""Migrations apply to a fresh database and roll back cleanly."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ALEMBIC_INI = os.path.join(REPO_ROOT, "infra", "db", "alembic.ini")


@pytest.fixture
def blank_database(admin_url: URL) -> Iterator[URL]:
    """A fresh, unmigrated database dropped at the end of the test."""
    db_name = f"argus_mig_{uuid.uuid4().hex[:12]}"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    try:
        yield admin_url.set(database=db_name)
    finally:
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


def _alembic(url: URL, target: str) -> None:
    previous = os.environ.get("ARGUS_MIGRATION_DATABASE_URL")
    os.environ["ARGUS_MIGRATION_DATABASE_URL"] = url.render_as_string(hide_password=False)
    try:
        config = Config(ALEMBIC_INI)
        if target == "base":
            command.downgrade(config, "base")
        else:
            command.upgrade(config, target)
    finally:
        if previous is None:
            os.environ.pop("ARGUS_MIGRATION_DATABASE_URL", None)
        else:
            os.environ["ARGUS_MIGRATION_DATABASE_URL"] = previous


def _counts(url: URL) -> dict[str, int]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                "tables": conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
                    )
                ).scalar_one(),
                "enums": conn.execute(
                    text(
                        "SELECT count(DISTINCT t.typname) FROM pg_type t "
                        "JOIN pg_enum e ON t.oid = e.enumtypid"
                    )
                ).scalar_one(),
                "triggers": conn.execute(
                    text("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal")
                ).scalar_one(),
                "guard_functions": conn.execute(
                    text("SELECT count(*) FROM pg_proc WHERE proname = 'argus_reject_mutation'")
                ).scalar_one(),
            }
    finally:
        engine.dispose()


def test_migration_applies_to_a_fresh_database(blank_database: URL):
    _alembic(blank_database, "head")
    counts = _counts(blank_database)
    assert counts["tables"] > 0
    assert counts["enums"] > 0
    assert counts["triggers"] > 0
    assert counts["guard_functions"] == 1


def test_downgrade_leaves_nothing_behind(blank_database: URL):
    """A partial rollback that strands enum types or triggers is not reversible."""
    _alembic(blank_database, "head")
    _alembic(blank_database, "base")

    counts = _counts(blank_database)
    assert counts == {"tables": 0, "enums": 0, "triggers": 0, "guard_functions": 0}


def test_migration_is_reapplicable_after_rollback(blank_database: URL):
    _alembic(blank_database, "head")
    _alembic(blank_database, "base")
    _alembic(blank_database, "head")

    assert _counts(blank_database)["tables"] > 0


def test_schema_matches_metadata_with_no_pending_changes(blank_database: URL):
    """Autogenerate finds nothing to do — the migration and the model agree."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from infra.db.schema import metadata

    _alembic(blank_database, "head")
    engine = create_engine(blank_database)
    try:
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), metadata)
    finally:
        engine.dispose()

    assert diff == [], f"Schema drift between migration and metadata: {diff}"
