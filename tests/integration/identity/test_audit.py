"""Every sensitive action reaches `audit_log`, and no credential does."""

from __future__ import annotations

import psycopg
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, InternalError

from infra.db.schema.users import audit_log
from services.identity import audit
from tests.integration.identity.conftest import GOOD_PASSWORD

SECRET = "a-memorable-passphrase-1234"


def _actions(connection, user_id=None) -> list[str]:
    query = select(audit_log.c.action).order_by(audit_log.c.occurred_at)
    if user_id is not None:
        query = query.where(audit_log.c.actor_user_id == user_id)
    return [row[0] for row in connection.execute(query).all()]


def test_registration_is_recorded(client, connection, signed_up):
    account = signed_up("audited")

    assert audit.REGISTERED in _actions(connection, account["user_id"])


def test_login_is_recorded_with_the_session_it_issued(client, connection, logged_in):
    account = logged_in("loginaudit")

    row = connection.execute(
        select(audit_log)
        .where(audit_log.c.actor_user_id == account["user_id"])
        .where(audit_log.c.action == audit.LOGIN)
    ).one()

    assert row.payload["session_id"] == account["session_id"]
    assert row.payload["mfa_used"] is False


def test_a_failed_login_is_recorded_too(client, connection, signed_up):
    """A trail that only holds successes describes a system where nothing went wrong."""
    account = signed_up("failaudit")
    client.post("/identity/login", json={"email": account["email"], "password": "wrong-one"})

    row = connection.execute(
        select(audit_log).where(audit_log.c.action == audit.LOGIN_FAILED)
    ).one()

    assert row.payload["reason"] == "bad_password"
    assert row.payload["email"] == account["email"]


def test_a_failed_login_against_no_account_has_no_actor(client, connection, email):
    """Nullable `actor_user_id` earning its keep: there is an attempt, not an actor."""
    ghost = email("phantom")
    client.post("/identity/login", json={"email": ghost, "password": GOOD_PASSWORD})

    row = connection.execute(
        select(audit_log).where(audit_log.c.action == audit.LOGIN_FAILED)
    ).one()

    assert row.actor_user_id is None
    assert row.payload["email"] == ghost
    assert row.payload["reason"] == "no_such_user"


def test_logout_is_recorded(client, connection, logged_in):
    account = logged_in("logoutaudit")
    client.post("/identity/logout", headers=account["headers"])

    assert audit.LOGOUT in _actions(connection, account["user_id"])


def test_a_password_change_is_recorded_with_the_sessions_it_ended(client, connection, logged_in):
    account = logged_in("pwaudit", password=SECRET)
    client.post("/identity/login", json={"email": account["email"], "password": SECRET})

    client.post(
        "/identity/password",
        headers=account["headers"],
        json={"current_password": SECRET, "new_password": "another-long-passphrase"},
    )

    row = connection.execute(
        select(audit_log).where(audit_log.c.action == audit.PASSWORD_CHANGED)
    ).one()
    assert row.actor_user_id == account["user_id"]
    assert row.payload["sessions_ended"] == 2


def test_mfa_enrolment_and_removal_are_recorded(client, connection, logged_in, enrol_mfa):
    account = logged_in("mfaaudit")
    enrol_mfa(account["headers"])
    client.delete("/identity/mfa/enroll", headers=account["headers"])

    actions = _actions(connection, account["user_id"])
    assert audit.MFA_ENROLLED in actions
    assert audit.MFA_DISABLED in actions


def test_a_deactivation_is_recorded_with_its_actor(
    client, connection, logged_in, promote, enrol_mfa
):
    victim = logged_in("deactivated")
    admin = logged_in("deactivator")
    promote(admin["user_id"], "admin")
    enrol_mfa(admin["headers"])

    removed = client.post(
        f"/identity/admin/users/{victim['user_id']}/deactivate", headers=admin["headers"]
    )

    assert removed.status_code == 204
    row = connection.execute(
        select(audit_log).where(audit_log.c.action == audit.SESSIONS_REVOKED)
    ).one()
    assert row.actor_user_id == admin["user_id"]
    assert row.entity_id == victim["user_id"]
    assert row.payload["reason"] == "account_deactivated"


def test_no_audit_payload_anywhere_contains_a_credential(client, connection, logged_in, enrol_mfa):
    """The sweep. Every action this module produces, then a search for secrets."""
    account = logged_in("sweepaudit", password=SECRET)
    secret = enrol_mfa(account["headers"])
    client.post("/identity/login", json={"email": account["email"], "password": "wrong"})
    client.post("/identity/logout", headers=account["headers"])

    written = "".join(str(row) for row in connection.execute(select(audit_log)).all())

    assert SECRET not in written
    assert secret not in written
    assert account["token"] not in written


def test_the_audit_writer_refuses_a_payload_that_names_a_secret(client, connection):
    """Raised, not filtered. A filter teaches callers that passing secrets is fine."""
    for key in ("password", "new_password", "mfa_secret", "session_token", "api_key"):
        with pytest.raises(audit.CredentialInPayload) as raised:
            audit.record(connection, audit.LOGIN, payload={key: "whatever"})
        assert key in str(raised.value)


def test_the_refusal_reaches_into_nested_payloads(client, connection):
    """A secret one level down is still a secret in an append-only table."""
    with pytest.raises(audit.CredentialInPayload):
        audit.record(
            connection,
            audit.LOGIN,
            payload={"context": {"supplied": {"password": "hunter2"}}},
        )


def test_an_action_outside_the_closed_set_is_refused(client, connection):
    """Adding a sensitive action should be a visible change, not a new string."""
    with pytest.raises(ValueError, match="not one of the recorded actions"):
        audit.record(connection, "user.did_something", payload={})


def test_the_audit_log_cannot_be_edited_or_deleted(client, connection, signed_up):
    """Module 03's guard, exercised against the rows this module writes."""
    account = signed_up("immutable")

    savepoint = connection.begin_nested()
    with pytest.raises((IntegrityError, InternalError)) as raised:
        connection.execute(
            audit_log.delete().where(audit_log.c.actor_user_id == account["user_id"])
        )
    assert isinstance(raised.value.orig, psycopg.errors.RestrictViolation)
    savepoint.rollback()

    savepoint = connection.begin_nested()
    with pytest.raises((IntegrityError, InternalError)) as raised:
        connection.execute(
            audit_log.update()
            .where(audit_log.c.actor_user_id == account["user_id"])
            .values(action=audit.LOGOUT)
        )
    assert isinstance(raised.value.orig, psycopg.errors.RestrictViolation)
    savepoint.rollback()


def test_the_source_address_is_recorded(client, connection, logged_in):
    """For the operator reading this after an incident."""
    account = logged_in("addressed")

    row = connection.execute(
        select(audit_log)
        .where(audit_log.c.actor_user_id == account["user_id"])
        .where(audit_log.c.action == audit.LOGIN)
    ).one()

    assert row.ip_address == "testclient"
