"""The health-check surface, including what it does when a dependency fails."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine, text

from infra.observability.freshness import FreshnessState
from infra.observability.health import Status, check_health, expected_revision
from tests.integration.observability.conftest import NOW


def test_a_working_database_with_fresh_data_is_ok(connection, register, add_bar):
    for feed_security in ("A", "B"):
        add_bar(register(feed_security), event_time=NOW - timedelta(hours=2))
    _fill_other_feeds(connection, register)

    report = check_health(connection=connection, now=NOW)

    assert report.status is Status.OK
    assert report.healthy is True
    assert report.http_status == 200
    assert {check.name for check in report.checks} == {
        "database",
        "migrations",
        "data_freshness",
    }


def test_the_migration_check_compares_against_the_real_head(connection):
    report = check_health(connection=connection, now=NOW)
    migrations = _check(report, "migrations")

    assert migrations.status is Status.OK
    assert migrations.data["current"] == migrations.data["expected"] == expected_revision()


def test_the_expected_revision_is_derived_from_the_migration_files():
    """Not hardcoded, so adding a migration cannot leave this pointing behind.

    A hardcoded head would make the check pass against a database the code
    cannot actually use, which is worse than not checking.
    """
    from pathlib import Path

    revisions = sorted(path.name for path in Path("infra/db/migrations/versions").glob("[0-9]*.py"))

    assert expected_revision() == revisions[-1].split("_", 1)[0]


# --------------------------------------------------------------------------
# Degraded: serving, but something wants a person
# --------------------------------------------------------------------------


def test_a_stale_feed_is_degraded_and_not_down(connection, register, add_bar):
    """The distinction a binary health check cannot make.

    A load balancer must not remove this instance: a stale feed is not
    fixed by having fewer servers, and removing them makes it worse.
    """
    add_bar(
        register("OLD"), event_time=NOW - timedelta(days=30), available_at=NOW - timedelta(days=30)
    )

    report = check_health(connection=connection, now=NOW)

    assert report.status is Status.DEGRADED
    assert report.healthy is False
    assert report.http_status == 200, "degraded instances stay in rotation"
    assert _check(report, "database").status is Status.OK


def test_a_never_ingested_feed_is_degraded_and_named(connection):
    """An empty database is degraded, which is the honest answer for one."""
    report = check_health(connection=connection, now=NOW)

    freshness = _check(report, "data_freshness")
    assert freshness.status is Status.DEGRADED
    assert set(freshness.data.values()) == {FreshnessState.UNAVAILABLE.value}


# --------------------------------------------------------------------------
# Down: simulated dependency failure
# --------------------------------------------------------------------------


def test_an_unreachable_database_is_down(admin_url):
    """The simulated failure the brief asks for.

    A real engine pointed at a port nothing is listening on, so the
    failure is a genuine connection error rather than a patched exception.
    """
    engine = create_engine("postgresql+psycopg://argus:nope@127.0.0.1:1/argus")

    report = check_health(engine)

    assert report.status is Status.DOWN
    assert report.http_status == 503
    assert _check(report, "database").status is Status.DOWN


def test_the_down_response_carries_no_connection_string(admin_url):
    """A DSN commonly holds a password, and this response is the one most
    likely to be served unauthenticated. So the exception type travels and
    its message does not.
    """
    engine = create_engine("postgresql+psycopg://argus:hunter2@127.0.0.1:1/argus")

    report = check_health(engine)
    body = str(report.as_dict())

    assert "hunter2" not in body
    assert "127.0.0.1" not in body
    assert _check(report, "database").data["error"]


def test_a_database_with_no_migrations_is_down(connection):
    """A schema the code does not match produces confusing failures everywhere."""
    savepoint = connection.begin_nested()
    connection.execute(text("DROP TABLE alembic_version"))

    report = check_health(connection=connection, now=NOW)

    assert report.status is Status.DOWN
    assert "never been migrated" in _check(report, "migrations").detail
    savepoint.rollback()


def test_a_database_on_the_wrong_revision_is_down(connection):
    savepoint = connection.begin_nested()
    connection.execute(text("UPDATE alembic_version SET version_num = '0001'"))

    report = check_health(connection=connection, now=NOW)

    migrations = _check(report, "migrations")
    assert report.status is Status.DOWN
    assert migrations.data["current"] == "0001"
    assert migrations.data["expected"] == expected_revision()
    savepoint.rollback()


def test_a_down_database_short_circuits_the_later_checks(connection):
    """Otherwise one outage reads as several separate problems."""
    savepoint = connection.begin_nested()
    connection.execute(text("DROP TABLE alembic_version"))

    report = check_health(connection=connection, now=NOW)

    assert {check.name for check in report.checks} == {"database", "migrations"}
    savepoint.rollback()


def test_calling_with_neither_engine_nor_connection_is_down_not_an_exception(connection):
    """A health check that raises is a health check that is down."""
    report = check_health()

    assert report.status is Status.DOWN
    assert report.checks[0].name == "database"


def test_the_report_serialises_for_a_monitoring_tool(connection):
    payload = check_health(connection=connection, now=NOW).as_dict()

    assert set(payload) == {"as_of", "status", "healthy", "checks"}
    assert all(set(check) == {"name", "status", "detail", "data"} for check in payload["checks"])


def test_the_overall_status_is_the_worst_of_its_parts(connection, register, add_bar):
    add_bar(
        register("STALE"),
        event_time=NOW - timedelta(days=40),
        available_at=NOW - timedelta(days=40),
    )

    report = check_health(connection=connection, now=NOW)

    assert _check(report, "database").status is Status.OK
    assert _check(report, "migrations").status is Status.OK
    assert _check(report, "data_freshness").status is Status.DEGRADED
    assert report.status is Status.DEGRADED


def _check(report, name):
    return next(check for check in report.checks if check.name == name)


def _fill_other_feeds(connection, register):
    """Give every watched feed a recent row, so freshness is OK overall."""
    from infra.db.schema.canonical import canonical_corporate_actions, canonical_fundamentals
    from infra.db.schema.news import canonical_news

    security_id = register("FILL")
    stamp = NOW - timedelta(hours=1)
    connection.execute(
        canonical_fundamentals.insert().values(
            security_id=security_id,
            statement_type="INCOME",
            fiscal_period="FY",
            fiscal_period_end=NOW.date(),
            event_time=stamp,
            observation_time=stamp,
            availability_time=stamp,
            ingestion_time=stamp,
            data={"revenue": 1},
        )
    )
    connection.execute(
        canonical_corporate_actions.insert().values(
            security_id=security_id,
            action_type="SPLIT",
            effective_date=stamp,
            event_time=stamp,
            observation_time=stamp,
            availability_time=stamp,
            ingestion_time=stamp,
            details={"numerator": 2, "denominator": 1},
        )
    )
    connection.execute(
        canonical_news.insert().values(
            security_id=security_id,
            headline="Something happened",
            url=f"https://example.test/{security_id}",
            source_site="example.test",
            event_time=stamp,
            observation_time=stamp,
            availability_time=stamp,
            ingestion_time=stamp,
        )
    )
