"""The restore drill, run as a test rather than described in a document.

A backup nobody has restored is a file. So this actually dumps a real
database, restores it into a real new one, and then asks the restored
copy to behave like ARGUS — because matching row counts are not the same
as a working system, and the difference is exactly where a bad backup
hides.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from psycopg import errors
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DatabaseError

from infra.db.append_only import APPEND_ONLY_TABLES, NO_DELETE_TABLES
from infra.deploy.backup import (
    DRILL_SUFFIX,
    IRREPLACEABLE_TABLES,
    drill,
    dump,
    restore,
    verify_restore,
)
from infra.deploy.migrate import upgrade_to_head

pytestmark = pytest.mark.skipif(
    shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
    reason="pg_dump/pg_restore not installed; the drill shells out to them",
)


def _dsn(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _seed(engine: Engine) -> None:
    """A few rows in tables that cannot be regenerated from anything."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_log (action, payload) VALUES "
                "('drill.seed', '{\"n\": 1}'::jsonb), ('drill.seed', '{\"n\": 2}'::jsonb)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO login_attempts (email, succeeded, reason) "
                "VALUES ('drill@argus.test', true, NULL)"
            )
        )


@pytest.fixture
def seeded(fresh_engine: Engine, alembic_target) -> Engine:
    upgrade_to_head(fresh_engine)
    _seed(fresh_engine)
    return fresh_engine


def test_the_drill_produces_a_usable_restore(
    seeded: Engine, fresh_database: URL, archive_dir, drop_database
):
    """Dump, restore, verify — the whole loop, against real databases."""
    drop_database(f"{fresh_database.database}{DRILL_SUFFIX}")

    result = drill(_dsn(fresh_database), archive=archive_dir / "drill.dump")

    assert result.passed
    assert result.backup.bytes_written > 0
    assert result.verification.schema_current
    assert result.verification.missing_irreplaceable == ()


def test_the_restored_copy_holds_the_same_rows(
    seeded: Engine, fresh_database: URL, archive_dir, drop_database
):
    drop_database(f"{fresh_database.database}{DRILL_SUFFIX}")
    result = drill(_dsn(fresh_database), archive=archive_dir / "drill.dump")

    assert result.verification.row_counts["audit_log"] == 2
    assert result.verification.row_counts["login_attempts"] == 1


def test_the_restored_copy_still_refuses_a_delete(
    seeded: Engine, fresh_database: URL, archive_dir, drop_database
):
    """The check most likely to catch a quiet failure.

    A restore that dropped the append-only triggers behaves completely
    normally and has silently lost the project's central guarantee. Row
    counts would match; the guarantee would be gone.
    """
    target = drop_database(f"{fresh_database.database}{DRILL_SUFFIX}")
    drill(_dsn(fresh_database), archive=archive_dir / "drill.dump")

    restored = create_engine(fresh_database.set(database=target))
    try:
        with pytest.raises(DatabaseError) as rejected, restored.begin() as connection:
            connection.execute(text("DELETE FROM audit_log"))
        assert isinstance(rejected.value.orig, errors.RestrictViolation)

        with pytest.raises(DatabaseError) as truncated, restored.begin() as connection:
            connection.execute(text("TRUNCATE audit_log"))
        assert isinstance(truncated.value.orig, errors.RestrictViolation)
    finally:
        restored.dispose()


def test_verification_counts_both_triggers_on_every_guarded_table(
    seeded: Engine, fresh_database: URL, archive_dir, drop_database
):
    """Row-level and TRUNCATE. A table carrying one and not the other is
    erasable by a single statement while looking completely protected."""
    drop_database(f"{fresh_database.database}{DRILL_SUFFIX}")
    result = drill(_dsn(fresh_database), archive=archive_dir / "drill.dump")

    expected = (len(APPEND_ONLY_TABLES) + len(NO_DELETE_TABLES)) * 2
    assert result.verification.expected_guard_triggers == expected
    assert result.verification.guard_triggers == expected


def test_a_restore_missing_an_irreplaceable_table_is_not_usable(
    seeded: Engine, fresh_database: URL, archive_dir, drop_database
):
    """The failure `verify_restore` exists for: a restore that looks fine.

    Provoked by restoring only the schema of one table's absence — here,
    by dropping `user_watchlists` from the restored copy and re-verifying.
    A database missing it serves every request that does not touch a
    watchlist, which is almost all of them.
    """
    target = drop_database(f"{fresh_database.database}{DRILL_SUFFIX}")
    drill(_dsn(fresh_database), archive=archive_dir / "drill.dump")

    restored = create_engine(fresh_database.set(database=target))
    try:
        with restored.begin() as connection:
            connection.execute(text("DROP TABLE user_watchlist_items"))
            connection.execute(text("DROP TABLE user_watchlists"))
        verification = verify_restore(restored)
    finally:
        restored.dispose()

    assert not verification.usable
    assert "user_watchlists" in verification.missing_irreplaceable


def test_the_drill_refuses_to_restore_over_its_own_source(fresh_database: URL):
    """A drill that can be aimed at production by a typo is not a drill."""
    with pytest.raises(ValueError, match="Refusing"):
        drill(_dsn(fresh_database), target_database=fresh_database.database)


def test_every_irreplaceable_table_is_one_the_schema_actually_has(
    seeded: Engine, fresh_engine: Engine
):
    """A typo in `IRREPLACEABLE_TABLES` would report a restore as broken forever."""
    with fresh_engine.connect() as connection:
        present = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
        }
    assert set(IRREPLACEABLE_TABLES) <= present


def test_the_dump_carries_schema_and_data_together(
    seeded: Engine, fresh_database: URL, archive_dir
):
    """A data-only dump would not carry the append-only triggers.

    Read back from the archive itself rather than from a restored copy,
    so this fails on the `pg_dump` flags rather than on anything later.
    """
    archive = archive_dir / "listing.dump"
    dump(_dsn(fresh_database), archive)

    listing = subprocess.run(
        ["pg_restore", "--list", str(archive)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "TRIGGER" in listing
    assert "FUNCTION" in listing
    assert "TABLE DATA" in listing


def test_restore_needs_an_empty_target(
    seeded: Engine, fresh_database: URL, archive_dir, drop_database
):
    """Restoring over a populated database is how a drill becomes an incident."""
    target = drop_database(f"{fresh_database.database}{DRILL_SUFFIX}")
    archive = archive_dir / "twice.dump"
    dump(_dsn(fresh_database), archive)

    drill(_dsn(fresh_database), archive=archive_dir / "first.dump")

    with pytest.raises(RuntimeError, match="pg_restore failed"):
        restore(archive, _dsn(fresh_database.set(database=target)))
