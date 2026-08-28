"""The health endpoint: a public liveness bit, and an admin-gated detailed view."""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from infra.security.health_app import create_health_app, get_connection
from services.identity import accounts


@pytest.fixture
def health(engine, connection):
    """`/health/live` genuinely needs a working engine — see `health_app.py`
    on why it does not go through the overridable connection the way
    every other route does. `/health/detail` still sees this test's
    fixtures via the dependency override below.
    """
    app = create_health_app(engine)

    def _connection():
        savepoint = connection.begin_nested()
        try:
            yield connection
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise

    app.dependency_overrides[get_connection] = _connection
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# --------------------------------------------------------------------------
# Liveness: public, minimal
# --------------------------------------------------------------------------


def test_liveness_works_with_no_credential_at_all(health):
    response = health.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "up"}


def test_liveness_reports_no_detail_whatsoever(health):
    """Reconnaissance value withheld: no revision, no feed names, no states."""
    response = health.get("/health/live")

    body = response.json()
    assert set(body) == {"status"}
    for leak in ("migration", "revision", "feed", "ohlcv", "news", "fundamentals"):
        assert leak not in response.text.lower()


def test_liveness_reports_down_when_the_database_is_unreachable():
    from sqlalchemy import create_engine

    dead_engine = create_engine("postgresql+psycopg://argus:nope@127.0.0.1:1/argus")
    app = create_health_app(dead_engine)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/health/live")

    assert response.status_code == 503
    assert response.json() == {"status": "down"}


def test_liveness_still_reports_up_when_only_a_feed_is_stale(health, register, connection):
    """A stale feed is DEGRADED, not DOWN — liveness must not conflate them."""
    from datetime import datetime

    from data.canonical_model.records import CanonicalTimeframe
    from infra.db.schema.canonical import canonical_ohlcv

    security_id = register("STALEFEED")
    old = datetime.now(UTC) - timedelta(days=90)
    connection.execute(
        canonical_ohlcv.insert().values(
            security_id=security_id,
            timeframe=CanonicalTimeframe.DAILY.value,
            event_time=old,
            observation_time=old,
            availability_time=old,
            ingestion_time=old,
            open_raw=1,
            high_raw=1,
            low_raw=1,
            close_raw=1,
            volume_raw=1,
        )
    )

    response = health.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "up"}


# --------------------------------------------------------------------------
# Detail: admin-gated
# --------------------------------------------------------------------------


def test_the_detail_view_requires_a_credential(health):
    response = health.get("/health/detail")

    assert response.status_code == 401


def test_the_detail_view_refuses_a_registered_user(health, logged_in):
    account = logged_in("ordinary")

    response = health.get("/health/detail", headers=account["headers"])

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_the_detail_view_requires_the_admins_second_factor_too(health, logged_in, promote):
    """The exact gate `services/identity/roles.py` already established, reused."""
    admin = logged_in("bareadmin")
    promote(admin["user_id"], "admin")

    response = health.get("/health/detail", headers=admin["headers"])

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MFA_REQUIRED"


def test_an_admin_with_a_second_factor_sees_the_full_detail(health, logged_in, promote, enrol_mfa):
    admin = logged_in("fulladmin")
    promote(admin["user_id"], "admin")
    enrol_mfa(admin["headers"])

    response = health.get("/health/detail", headers=admin["headers"])

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"as_of", "status", "healthy", "checks"}
    names = {check["name"] for check in body["checks"]}
    assert {"database", "migrations", "data_freshness"} <= names


def test_the_detail_view_reports_a_stale_feed_the_liveness_probe_did_not(
    health, logged_in, promote, enrol_mfa, register, connection
):
    from datetime import datetime

    from data.canonical_model.records import CanonicalTimeframe
    from infra.db.schema.canonical import canonical_ohlcv

    security_id = register("DETAILSTALE")
    old = datetime.now(UTC) - timedelta(days=90)
    connection.execute(
        canonical_ohlcv.insert().values(
            security_id=security_id,
            timeframe=CanonicalTimeframe.DAILY.value,
            event_time=old,
            observation_time=old,
            availability_time=old,
            ingestion_time=old,
            open_raw=1,
            high_raw=1,
            low_raw=1,
            close_raw=1,
            volume_raw=1,
        )
    )
    admin = logged_in("detailadmin")
    promote(admin["user_id"], "admin")
    enrol_mfa(admin["headers"])

    detail = health.get("/health/detail", headers=admin["headers"])
    live = health.get("/health/live")

    assert detail.json()["status"] == "degraded"
    freshness = next(c for c in detail.json()["checks"] if c["name"] == "data_freshness")
    assert freshness["status"] == "degraded"
    assert live.json() == {"status": "up"}, "liveness still says up for a degraded instance"


def test_the_detail_view_reports_down_on_a_wrong_migration_revision(
    health, logged_in, promote, enrol_mfa, connection
):
    admin = logged_in("migrationadmin")
    promote(admin["user_id"], "admin")
    enrol_mfa(admin["headers"])

    savepoint = connection.begin_nested()
    connection.execute(text("UPDATE alembic_version SET version_num = '0001'"))

    response = health.get("/health/detail", headers=admin["headers"])

    assert response.status_code == 503
    assert response.json()["status"] == "down"
    savepoint.rollback()


def test_promoting_a_user_and_then_rotating_their_session_locks_them_out_of_detail(
    health, logged_in, promote, enrol_mfa, connection
):
    """Module 24's own session-rotation feature, exercised against its own new endpoint."""
    admin = logged_in("revoked-admin")
    promote(admin["user_id"], "admin")
    enrol_mfa(admin["headers"])
    assert health.get("/health/detail", headers=admin["headers"]).status_code == 200

    accounts.change_role(connection, admin["user_id"], "admin")  # re-affirm -> revokes

    after = health.get("/health/detail", headers=admin["headers"])
    assert after.status_code == 401
