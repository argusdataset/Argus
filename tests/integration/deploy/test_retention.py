"""Retention against a real database, including what it cannot do.

The interesting half of this module is negative: three tables that grow
without bound and cannot be pruned, because the guard that makes them
trustworthy is the same guard that stops a retention job. That is a
constraint worth proving rather than asserting in a comment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from psycopg import errors
from sqlalchemy import Engine, text
from sqlalchemy.exc import DatabaseError

from infra.deploy.migrate import upgrade_to_head
from infra.deploy.retention import (
    MONITORED_TABLES,
    SESSION_RETENTION_DAYS,
    measure_growth,
    prune_expired_sessions,
    run_retention,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def migrated(fresh_engine: Engine, alembic_target) -> Engine:
    upgrade_to_head(fresh_engine)
    return fresh_engine


def _user(engine: Engine) -> str:
    with engine.begin() as connection:
        role = connection.execute(
            text("INSERT INTO roles (name, description) VALUES ('drill', 'x') RETURNING id")
        ).scalar_one()
        return str(
            connection.execute(
                text(
                    "INSERT INTO users (email, password_hash, role_id) "
                    "VALUES ('drill@argus.test', 'x', :role) RETURNING id"
                ),
                {"role": role},
            ).scalar_one()
        )


def _session(engine: Engine, user_id: str, *, issued: datetime, expires: datetime) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sessions (user_id, token_hash, issued_at, expires_at) "
                "VALUES (:user, :token, :issued, :expires)"
            ),
            {
                "user": user_id,
                "token": f"hash-{issued.isoformat()}",
                "issued": issued,
                "expires": expires,
            },
        )


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


# --------------------------------------------------------------------------
# The one prune that is both legal and right
# --------------------------------------------------------------------------


def test_a_live_session_is_not_pruned(migrated: Engine):
    user = _user(migrated)
    _session(migrated, user, issued=NOW - timedelta(days=1), expires=NOW + timedelta(days=29))

    assert prune_expired_sessions(migrated, now=NOW) == 0
    assert _count(migrated, "sessions") == 1


def test_a_session_expired_inside_the_window_is_kept(migrated: Engine):
    """So a support question about yesterday's logout is still answerable."""
    user = _user(migrated)
    _session(migrated, user, issued=NOW - timedelta(days=40), expires=NOW - timedelta(days=5))

    assert prune_expired_sessions(migrated, now=NOW) == 0
    assert _count(migrated, "sessions") == 1


def test_a_session_expired_past_the_window_is_deleted(migrated: Engine):
    user = _user(migrated)
    _session(
        migrated,
        user,
        issued=NOW - timedelta(days=120),
        expires=NOW - timedelta(days=SESSION_RETENTION_DAYS + 1),
    )

    assert prune_expired_sessions(migrated, now=NOW) == 1
    assert _count(migrated, "sessions") == 0


def test_pruning_a_session_does_not_erase_the_login_that_created_it(migrated: Engine):
    """An expired session row carries no history the append-only tables lack.

    That is the argument for pruning it, so it is worth checking that
    the history really is somewhere else.
    """
    user = _user(migrated)
    _session(
        migrated,
        user,
        issued=NOW - timedelta(days=120),
        expires=NOW - timedelta(days=90),
    )
    with migrated.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO login_attempts (email, user_id, succeeded) "
                "VALUES ('drill@argus.test', :user, true)"
            ),
            {"user": user},
        )

    prune_expired_sessions(migrated, now=NOW)

    assert _count(migrated, "sessions") == 0
    assert _count(migrated, "login_attempts") == 1


# --------------------------------------------------------------------------
# The three that cannot be pruned, and why
# --------------------------------------------------------------------------


ROW_SQL = {
    "audit_log": "INSERT INTO audit_log (action, payload) VALUES ('drill', '{}'::jsonb)",
    "login_attempts": (
        "INSERT INTO login_attempts (email, succeeded) VALUES ('drill@argus.test', true)"
    ),
    "registration_attempts": (
        "INSERT INTO registration_attempts (ip_address, succeeded) VALUES ('10.0.0.1', true)"
    ),
}


@pytest.mark.parametrize("table", sorted(MONITORED_TABLES))
def test_a_retention_job_cannot_delete_from_a_monitored_table(migrated: Engine, table: str):
    """This is the constraint, demonstrated rather than described.

    Making the obvious retention job work means dropping the guard — and
    a lockout whose evidence can be deleted is not a lockout.

    A row has to exist first. The guard is `FOR EACH ROW`, so a DELETE
    against an empty table fires nothing and succeeds vacuously — which
    is correct (it erased nothing) and is exactly the shape of test that
    passes while proving nothing.
    """
    with migrated.begin() as connection:
        connection.execute(text(ROW_SQL[table]))

    with pytest.raises(DatabaseError) as rejected, migrated.begin() as connection:
        connection.execute(text(f"DELETE FROM {table}"))
    assert isinstance(rejected.value.orig, errors.RestrictViolation)


@pytest.mark.parametrize("table", sorted(MONITORED_TABLES))
def test_a_monitored_table_cannot_be_truncated_even_when_empty(migrated: Engine, table: str):
    """TRUNCATE does not fire row-level triggers, so it needs its own guard.

    Deliberately run against an empty table, which is the case the
    row-level guard cannot see: `FOR EACH STATEMENT` fires regardless of
    how many rows there are, and a table whose row guard survived a
    restore while its TRUNCATE guard did not would be erasable by a
    single statement while looking completely protected.
    """
    with pytest.raises(DatabaseError) as rejected, migrated.begin() as connection:
        connection.execute(text(f"TRUNCATE {table}"))
    assert isinstance(rejected.value.orig, errors.RestrictViolation)


# --------------------------------------------------------------------------
# What is done instead: measurement
# --------------------------------------------------------------------------


def test_growth_is_measured_for_every_monitored_table(migrated: Engine):
    measurements = {item.table: item for item in measure_growth(migrated, now=NOW)}
    assert set(measurements) == set(MONITORED_TABLES)
    for measurement in measurements.values():
        assert measurement.bytes_on_disk > 0
        assert not measurement.over_threshold


def test_an_empty_table_projects_nothing_rather_than_projecting_zero(migrated: Engine):
    """There is no arrival rate to extrapolate from, and inventing one is worse."""
    for measurement in measure_growth(migrated, now=NOW):
        assert measurement.rows == 0
        assert measurement.days_to_warning is None


def test_a_rate_is_only_computed_once_the_data_spans_a_day(migrated: Engine):
    """Rows all landing inside one hour would project alarming nonsense."""
    with migrated.begin() as connection:
        for offset in (0, 30):
            connection.execute(
                text(
                    "INSERT INTO audit_log (occurred_at, action, payload) "
                    "VALUES (:at, 'drill', '{}'::jsonb)"
                ),
                {"at": NOW - timedelta(days=offset)},
            )

    measurements = {item.table: item for item in measure_growth(migrated, now=NOW)}
    audit = measurements["audit_log"]
    assert audit.rows == 2
    assert audit.rows_per_day == pytest.approx(2 / 30, rel=0.01)
    assert audit.days_to_warning is not None
    assert measurements["login_attempts"].days_to_warning is None


def test_a_full_pass_prunes_and_measures(migrated: Engine):
    user = _user(migrated)
    _session(migrated, user, issued=NOW - timedelta(days=120), expires=NOW - timedelta(days=90))

    result = run_retention(migrated, now=NOW)

    assert result["sessions_removed"] == 1
    assert len(result["growth"]) == len(MONITORED_TABLES)
