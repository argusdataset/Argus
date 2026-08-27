"""Registration, login, an authenticated request, logout. The whole loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from infra.db.schema.users import sessions as sessions_table
from services.identity import sessions
from tests.integration.identity.conftest import GOOD_PASSWORD


def test_register_login_use_logout(client, signed_up):
    """The round trip the brief asks for, in one test, end to end."""
    account = signed_up("roundtrip")

    signed_in = client.post(
        "/identity/login",
        json={"email": account["email"], "password": account["password"]},
    )
    assert signed_in.status_code == 200
    session = signed_in.json()
    assert session["token_type"] == "Bearer"
    headers = {"Authorization": f"Bearer {session['token']}"}

    me = client.get("/identity/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["user_id"] == str(account["user_id"])
    assert me.json()["email"] == account["email"]
    assert me.json()["role"] == "registered_user"

    assert client.post("/identity/logout", headers=headers).status_code == 204

    after = client.get("/identity/me", headers=headers)
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "SESSION_INVALID"


def test_registration_does_not_hand_back_a_session(client, signed_up):
    """Signing in is a separate act with its own lockout and its own audit record."""
    account = signed_up("separate")

    assert "token" not in account
    assert "session_id" not in account


def test_a_new_account_gets_registered_user_and_no_second_factor(client, signed_up):
    account = signed_up("defaults")

    assert account["role"] == "registered_user"
    assert account["mfa_enabled"] is False
    assert account["is_active"] is True


def test_the_same_address_cannot_register_twice(client, signed_up):
    account = signed_up("dupe")

    again = client.post(
        "/identity/register",
        json={"email": account["email"].upper(), "password": GOOD_PASSWORD},
    )

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "EMAIL_TAKEN"


def test_a_short_password_is_refused_before_an_account_exists(client, email):
    address = email("weak")
    response = client.post("/identity/register", json={"email": address, "password": "short"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WEAK_PASSWORD"
    assert response.json()["error"]["detail"]["minimum_length"] == 12

    # And no half-made account was left behind.
    login = client.post("/identity/login", json={"email": address, "password": "short"})
    assert login.status_code == 401


def test_a_wrong_password_and_an_unknown_address_are_indistinguishable(client, signed_up, email):
    """The account-existence oracle, closed. Same code, same status, same message.

    A caller who can tell these apart stops guessing passwords against ten
    thousand addresses and starts guessing them against the two hundred
    that answered differently.
    """
    account = signed_up("oracle")

    wrong = client.post(
        "/identity/login", json={"email": account["email"], "password": "wrong-" + GOOD_PASSWORD}
    )
    unknown = client.post(
        "/identity/login", json={"email": email("ghost"), "password": GOOD_PASSWORD}
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_two_logins_produce_two_independent_sessions(client, logged_in):
    """Signing in on a second device does not end the first."""
    first = logged_in("multi")
    second = client.post(
        "/identity/login",
        json={"email": first["email"], "password": first["password"]},
    ).json()

    assert second["token"] != first["token"]
    assert second["session_id"] != first["session_id"]

    assert client.post("/identity/logout", headers=first["headers"]).status_code == 204
    assert (
        client.get(
            "/identity/me", headers={"Authorization": f"Bearer {second['token']}"}
        ).status_code
        == 200
    )


def test_logging_out_twice_is_not_an_error(client, logged_in):
    """Somebody pressing sign-out twice should see success twice."""
    account = logged_in("twice")

    assert client.post("/identity/logout", headers=account["headers"]).status_code == 204
    assert client.post("/identity/logout", headers=account["headers"]).status_code == 204


def test_listing_your_own_sessions_never_includes_a_token(client, logged_in):
    """There is no endpoint in ARGUS that hands back a live credential."""
    account = logged_in("listing")

    body = client.get("/identity/sessions", headers=account["headers"])

    assert body.status_code == 200
    listed = body.json()
    assert len(listed) == 1
    assert listed[0]["active"] is True
    assert "token" not in listed[0]
    assert "token_hash" not in listed[0]
    assert account["token"] not in body.text


def test_the_stored_session_row_holds_a_hash_and_not_the_token(client, logged_in, connection):
    """Module 03's column comment, enforced: 'Hash, never the token itself'."""
    account = logged_in("hashed")

    row = connection.execute(
        select(sessions_table).where(sessions_table.c.id == account["session_id"])
    ).one()

    assert row.token_hash != account["token"]
    assert account["token"] not in row.token_hash
    assert row.token_hash == __import__(
        "services.identity.tokens", fromlist=["hash_token"]
    ).hash_token(account["token"])


def test_a_missing_or_malformed_authorization_header_is_a_401(client, logged_in):
    account = logged_in("headers")

    assert client.get("/identity/me").status_code == 401
    assert client.get("/identity/me", headers={"Authorization": "Bearer"}).status_code == 401
    assert client.get("/identity/me", headers={"Authorization": "Basic abc"}).status_code == 401
    assert (
        client.get(
            "/identity/me", headers={"Authorization": f"Bearer {account['token']}x"}
        ).status_code
        == 401
    )


def test_a_forged_token_naming_no_session_is_refused(client):
    """A random token is not a session, however well-formed it looks."""
    import secrets

    response = client.get(
        "/identity/me",
        headers={"Authorization": f"Bearer {secrets.token_urlsafe(32)}"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_INVALID"


def test_a_session_stops_working_once_it_expires(client, connection, logged_in):
    """Expiry is enforced by the query, not by anybody remembering to check."""
    account = logged_in("expiring")
    assert client.get("/identity/me", headers=account["headers"]).status_code == 200

    _age(connection, account["session_id"])

    after = client.get("/identity/me", headers=account["headers"])
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "SESSION_INVALID"


def test_an_expired_and_a_revoked_session_are_reported_identically(client, connection, logged_in):
    """One answer for every failure — see errors.py on why."""
    expired = logged_in("exp")
    revoked = logged_in("rev")

    _age(connection, expired["session_id"])
    client.post("/identity/logout", headers=revoked["headers"])

    first = client.get("/identity/me", headers=expired["headers"])
    second = client.get("/identity/me", headers=revoked["headers"])

    assert first.status_code == second.status_code == 401
    assert first.json() == second.json()


def test_revocation_marks_the_row_rather_than_deleting_it(client, connection, logged_in):
    """A deleted session is indistinguishable from one that never existed."""
    account = logged_in("kept")
    client.post("/identity/logout", headers=account["headers"])

    row = connection.execute(
        select(sessions_table).where(sessions_table.c.id == account["session_id"])
    ).one_or_none()

    assert row is not None, "the row survives the logout"
    assert row.revoked_at is not None


def test_deactivating_an_account_kills_its_live_sessions(client, connection, logged_in, promote):
    """A deactivation that takes effect tomorrow is not a deactivation."""
    victim = logged_in("victim")
    admin = logged_in("admin")
    promote(admin["user_id"], "admin")

    # Verified directly rather than through the admin route, which needs
    # a second factor — that gate has its own test.
    from services.identity import accounts

    accounts.deactivate(connection, victim["user_id"])

    assert client.get("/identity/me", headers=victim["headers"]).status_code == 401
    assert sessions.verify(connection, victim["token"]) is None


def _age(connection, session_id) -> None:
    """Move one session wholly into the past, as elapsed time would.

    Both timestamps, not just `expires_at`. Module 03 put a
    `expires_at > issued_at` check on this table, so a session cannot be
    expired by editing one column — which is the constraint working: it
    refuses to represent a session that expired before it was issued, and
    that includes a test trying to fake one.
    """
    now = datetime.now(UTC)
    connection.execute(
        sessions_table.update()
        .where(sessions_table.c.id == session_id)
        .values(
            issued_at=now - timedelta(hours=13),
            expires_at=now - timedelta(hours=1),
        )
    )
