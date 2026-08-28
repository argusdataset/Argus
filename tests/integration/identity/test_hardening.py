"""Module 24's narrow, targeted additions to Module 22's own logic:
rehashing an outdated password at login, per-source registration limits,
and session rotation on a role change.
"""

from __future__ import annotations

import time
from dataclasses import replace
from uuid import uuid4

from sqlalchemy import select

from infra.db.schema.users import registration_attempts
from services.identity import accounts, sessions
from services.identity.config import IdentitySettings
from services.identity.passwords import hash_password, needs_rehash, verify_password
from services.identity.roles import ADMIN, REGISTERED_USER
from tests.integration.identity.conftest import GOOD_PASSWORD

# --------------------------------------------------------------------------
# needs_rehash, wired at login
# --------------------------------------------------------------------------


def _weak_settings() -> IdentitySettings:
    """A cost lower than the real default — enough to make `needs_rehash` true.

    Kept low deliberately so hashing under it is fast; only the fact that
    it is *lower than current* matters for these tests, not its absolute
    strength.
    """
    base = IdentitySettings()
    return replace(base, argon2_memory_kib=replace(base.argon2_memory_kib, value=8192.0))


def test_a_hash_made_under_weaker_parameters_is_upgraded_on_login(
    client, connection, signed_up, stored_hash
):
    """The whole point: nothing calls `needs_rehash` except `log_in`, until now."""
    weak = _weak_settings()
    account = signed_up("outdated", password=GOOD_PASSWORD)
    original = stored_hash(account["user_id"])
    assert original is not None

    # Force the account onto a weak hash, the way a real one would have
    # gotten there: made under yesterday's parameters.
    from infra.db.schema.users import users

    connection.execute(
        users.update()
        .where(users.c.id == account["user_id"])
        .values(password_hash=hash_password(GOOD_PASSWORD, settings=weak))
    )
    downgraded = stored_hash(account["user_id"])
    assert needs_rehash(downgraded, settings=IdentitySettings()) is True

    accounts.log_in(connection, email=account["email"], password=GOOD_PASSWORD)

    upgraded = stored_hash(account["user_id"])
    assert upgraded != downgraded
    assert needs_rehash(upgraded, settings=IdentitySettings()) is False
    assert verify_password(GOOD_PASSWORD, upgraded)


def test_a_hash_already_at_current_parameters_is_left_alone(
    client, connection, signed_up, stored_hash
):
    """No rehash, no write, on the ordinary path — checked, not assumed."""
    account = signed_up("current")
    before = stored_hash(account["user_id"])

    accounts.log_in(connection, email=account["email"], password=GOOD_PASSWORD)

    after = stored_hash(account["user_id"])
    assert after == before, "an up-to-date hash must not be rewritten on every login"


def test_a_wrong_password_never_triggers_a_rehash(client, connection, signed_up, stored_hash):
    """The rehash sits strictly after verification succeeds — never on a refusal."""
    account = signed_up("guarded")
    before = stored_hash(account["user_id"])

    response = client.post(
        "/identity/login", json={"email": account["email"], "password": "wrong-one"}
    )

    assert response.status_code == 401
    assert stored_hash(account["user_id"]) == before


def test_rehashing_does_not_disturb_module_22s_timing_parity(client):
    """Re-run of Module 22's own guarantee, against this module's changes.

    `verify_password` is called identically whether or not a rehash later
    happens — the rehash runs after it returns, never inside the branch
    the timing test measures — so this must still hold exactly as Module
    22 left it.
    """
    stored = hash_password(GOOD_PASSWORD)
    verify_password("warm", None)  # prime the dummy-hash cache

    def median(hash_value):
        samples = []
        for _ in range(7):
            started = time.perf_counter()
            verify_password("a-guess-that-is-wrong", hash_value)
            samples.append(time.perf_counter() - started)
        return sorted(samples)[len(samples) // 2]

    missing = median(None)
    wrong = median(stored)

    assert 0.5 < (missing / wrong) < 2.0, f"missing={missing:.4f}s wrong={wrong:.4f}s"


# --------------------------------------------------------------------------
# Registration rate limiting, per source address
# --------------------------------------------------------------------------


LIMIT = int(IdentitySettings().max_registrations_per_address)


def _register(client, email: str) -> int:
    return client.post(
        "/identity/register", json={"email": email, "password": GOOD_PASSWORD}
    ).status_code


def test_repeated_registrations_from_one_address_are_locked_out(client, email):
    """Driven through the real endpoint, the same rigor as Module 22's brute-force tests."""
    statuses = [_register(client, email(f"spam{i}")) for i in range(LIMIT)]
    assert statuses == [201] * LIMIT

    locked = client.post(
        "/identity/register",
        json={"email": email("onemore"), "password": GOOD_PASSWORD},
    )

    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "REGISTRATION_LOCKED"


def test_the_lock_names_when_it_lifts(client, email):
    for i in range(LIMIT):
        _register(client, email(f"named{i}"))

    locked = client.post(
        "/identity/register", json={"email": email("named-x"), "password": GOOD_PASSWORD}
    )

    remaining = locked.json()["error"]["detail"]["retry_after_seconds"]
    assert 0 < remaining <= IdentitySettings().registration_lockout_seconds


def test_fewer_than_the_limit_are_not_locked(client, email):
    for i in range(LIMIT - 1):
        assert _register(client, email(f"under{i}")) == 201

    assert _register(client, email("under-last")) == 201


def test_repeated_successful_signups_still_trip_the_limit(client, email):
    """The design point this module got right on the second try.

    Registration's limit counts every attempt, successful or not — unlike
    login's "failures since the last success" rule, which would reset to
    zero after each successful signup and provide no protection at all
    against exactly the volume attack being defended against, since a
    spammer's registrations mostly succeed.
    """
    statuses = [_register(client, email(f"allgood{i}")) for i in range(LIMIT)]
    assert statuses == [201] * LIMIT

    blocked = client.post(
        "/identity/register", json={"email": email("allgood-last"), "password": GOOD_PASSWORD}
    )
    assert blocked.status_code == 429


def test_a_weak_password_attempt_still_counts_toward_the_limit(client, email):
    """A rejected registration is still volume from that source."""
    address = email("weakattempts")
    for _ in range(LIMIT):
        response = client.post("/identity/register", json={"email": address, "password": "short"})
        assert response.status_code == 400

    blocked = client.post(
        "/identity/register", json={"email": email("weak-last"), "password": GOOD_PASSWORD}
    )
    assert blocked.status_code == 429


def test_registrations_from_two_source_addresses_have_independent_budgets(
    client, connection, email
):
    """A spray from one source must not lock out a different one.

    `TestClient` gives every HTTP call through `client` the same fixed
    peer address, so — the same way Module 22's own per-source spray test
    does — the attacking address's attempts are written directly rather
    than driven through a client that cannot vary its own IP.
    """
    from services.identity import attempts

    for _ in range(LIMIT):
        attempts.record_registration_attempt(connection, ip_address="203.0.113.9", succeeded=True)

    assert _register(client, email("bystander")) == 201


def test_every_registration_attempt_is_recorded_including_the_locked_one(client, connection, email):
    address_prefix = "recorded"
    for i in range(LIMIT):
        _register(client, email(f"{address_prefix}{i}"))
    client.post(
        "/identity/register",
        json={"email": email(f"{address_prefix}-over"), "password": GOOD_PASSWORD},
    )

    rows = connection.execute(select(registration_attempts)).all()
    assert len(rows) >= LIMIT + 1
    assert any(row.reason == "rate_limited" for row in rows)
    assert all(row.succeeded for row in rows if row.reason is None)


def test_the_registration_attempt_log_records_the_address_and_never_the_password(
    client, connection, email
):
    address = email("logged")
    _register(client, address)

    row = connection.execute(
        select(registration_attempts).where(registration_attempts.c.email == address)
    ).one()

    assert row.succeeded is True
    assert GOOD_PASSWORD not in str(row)


# --------------------------------------------------------------------------
# Session rotation on a role change
# --------------------------------------------------------------------------


def test_a_role_change_invalidates_every_existing_session(client, connection, logged_in):
    """Old session token rejected after the change — end to end, through the real API."""
    account = logged_in("promoted")
    assert client.get("/identity/me", headers=account["headers"]).status_code == 200

    accounts.change_role(connection, account["user_id"], ADMIN)

    after = client.get("/identity/me", headers=account["headers"])
    assert after.status_code == 401
    assert sessions.verify(connection, account["token"]) is None


def test_a_role_change_returns_how_many_sessions_it_ended(client, logged_in, connection):
    first = logged_in("multi-session")
    second = client.post(
        "/identity/login",
        json={"email": first["email"], "password": first["password"]},
    ).json()

    ended = accounts.change_role(connection, first["user_id"], ADMIN)

    assert ended == 2
    assert (
        client.get(
            "/identity/me", headers={"Authorization": f"Bearer {second['token']}"}
        ).status_code
        == 401
    )


def test_a_role_change_is_audited_with_the_previous_and_new_role(client, connection, logged_in):
    from infra.db.schema.users import audit_log
    from services.identity import audit

    account = logged_in("audited-role-change")

    accounts.change_role(connection, account["user_id"], ADMIN)

    row = connection.execute(
        select(audit_log).where(audit_log.c.action == audit.ROLE_CHANGED)
    ).one()
    assert row.entity_id == account["user_id"]
    assert row.payload["from_role"] == REGISTERED_USER
    assert row.payload["to_role"] == ADMIN


def test_a_role_change_lets_the_user_sign_back_in_under_the_new_role(client, connection, logged_in):
    """The point of the rotation: re-authenticate, don't just get locked out."""
    account = logged_in("resumes")

    accounts.change_role(connection, account["user_id"], ADMIN)

    signed_in = client.post(
        "/identity/login",
        json={"email": account["email"], "password": account["password"]},
    )
    assert signed_in.status_code == 200
    me = client.get(
        "/identity/me",
        headers={"Authorization": f"Bearer {signed_in.json()['token']}"},
    )
    assert me.json()["role"] == ADMIN


def test_a_role_change_with_no_existing_sessions_ends_zero_and_does_not_error(
    client, connection, signed_up
):
    account = signed_up("neversignedin")

    ended = accounts.change_role(connection, account["user_id"], ADMIN)

    assert ended == 0


def test_an_unknown_user_id_raises_rather_than_silently_doing_nothing(client, connection):
    """A silent no-op here would leave an audit row for a role change that
    never happened, on an account that never existed.
    """
    import pytest as _pytest

    with _pytest.raises(ValueError, match="No user"):
        accounts.change_role(connection, uuid4(), ADMIN)
