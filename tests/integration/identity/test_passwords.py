"""Passwords: never stored plainly, never logged, never fast to guess."""

from __future__ import annotations

import time

from sqlalchemy import select

from infra.db.schema.users import audit_log, login_attempts, users
from services.identity.config import IdentitySettings
from services.identity.passwords import hash_password, needs_rehash, verify_password
from tests.integration.identity.conftest import GOOD_PASSWORD

SECRET = "a-very-memorable-passphrase-42"


def test_the_stored_value_is_an_argon2id_hash_and_not_the_password(client, signed_up, stored_hash):
    """The column holds a hash. Checked against the column, not against a promise."""
    account = signed_up("stored", password=SECRET)

    stored = stored_hash(account["user_id"])

    assert stored is not None
    assert SECRET not in stored
    assert stored.startswith("$argon2id$")
    assert "m=19456,t=2,p=1" in stored, "the configured cost travels inside the hash"


def test_the_password_appears_nowhere_in_any_table_this_module_writes(
    client, connection, signed_up
):
    """Registration and a failed login, then a sweep of everything written.

    The rule is "never stored or logged in plaintext", and the way that
    rule usually breaks is not the password column — it is an audit
    payload or an error message written by somebody being helpful.
    """
    account = signed_up("sweep", password=SECRET)
    client.post("/identity/login", json={"email": account["email"], "password": SECRET + "-wrong"})
    client.post("/identity/login", json={"email": account["email"], "password": SECRET})

    written = "".join(str(row) for row in connection.execute(select(audit_log)).all()) + "".join(
        str(row) for row in connection.execute(select(login_attempts)).all()
    )
    user_row = str(connection.execute(select(users).where(users.c.id == account["user_id"])).one())

    for haystack in (written, user_row):
        assert SECRET not in haystack
        assert SECRET + "-wrong" not in haystack


def test_no_response_body_ever_echoes_the_password(client, signed_up, logged_in):
    """Including the error bodies, which are the ones that get screenshotted."""
    account = logged_in("echo", password=SECRET)

    bodies = [
        client.post(
            "/identity/register", json={"email": account["email"], "password": SECRET}
        ).text,
        client.post("/identity/login", json={"email": account["email"], "password": SECRET}).text,
        client.post("/identity/login", json={"email": account["email"], "password": "nope"}).text,
        client.get("/identity/me", headers=account["headers"]).text,
        client.post(
            "/identity/password",
            headers=account["headers"],
            json={"current_password": "wrong", "new_password": SECRET},
        ).text,
    ]

    for body in bodies:
        assert SECRET not in body


def test_the_weak_password_error_names_the_rule_and_not_the_password(client, email):
    """This message reaches logs. It carries a number, not a credential."""
    response = client.post(
        "/identity/register", json={"email": email("weak"), "password": "abc123"}
    )

    body = response.text
    assert "abc123" not in body
    assert "12" in body


def test_the_hash_is_slow_enough_to_matter(client):
    """Not a fast general-purpose hash, measured rather than asserted in prose.

    SHA-256 over a password takes on the order of a microsecond, which is
    what makes an offline attack on a stolen table trivial. Argon2id at
    the configured cost is four orders of magnitude slower. The floor is
    set well under the real figure (~25ms here) so this does not become a
    flaky test on a loaded machine — it is checking a category, not a
    number.
    """
    started = time.perf_counter()
    stored = hash_password(GOOD_PASSWORD)
    hashing = time.perf_counter() - started

    started = time.perf_counter()
    assert verify_password(GOOD_PASSWORD, stored)
    verifying = time.perf_counter() - started

    assert hashing > 0.005, f"hashing took {hashing * 1000:.2f}ms — too fast to be a KDF"
    assert verifying > 0.005, f"verifying took {verifying * 1000:.2f}ms"


def test_the_configured_cost_is_the_owasp_profile_and_is_memory_hard(client):
    """Memory cost is the parameter that stops GPU cracking. It is set, not defaulted."""
    settings = IdentitySettings()

    assert int(settings.argon2_memory_kib) >= 19456
    assert int(settings.argon2_time_cost) >= 2
    assert "argon2_memory_kib" in settings.security()


def test_two_identical_passwords_produce_different_hashes(client):
    """Per-hash salt. Without it, a stolen table shows who shares a password."""
    first = hash_password(GOOD_PASSWORD)
    second = hash_password(GOOD_PASSWORD)

    assert first != second
    assert verify_password(GOOD_PASSWORD, first)
    assert verify_password(GOOD_PASSWORD, second)


def test_verifying_against_no_stored_hash_is_false_and_not_an_error(client):
    """The shape returned when the email named no account."""
    assert verify_password(GOOD_PASSWORD, None) is False


def test_a_missing_account_costs_the_same_as_a_wrong_password(client):
    """The timing oracle, closed. Same work either way.

    Without the dummy verification, a non-existent account answers in
    microseconds and a real one in tens of milliseconds — a remote
    account-existence oracle that no amount of care in the error messages
    can close.
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

    # Within a factor of two: the point is that both pay for a real
    # argon2 verification, not that they are identical to the microsecond.
    assert 0.5 < (missing / wrong) < 2.0, f"missing={missing:.4f}s wrong={wrong:.4f}s"


def test_changing_a_password_requires_the_current_one(client, logged_in):
    """A session proves somebody signed in once, not that this person knows the password."""
    account = logged_in("change", password=SECRET)

    refused = client.post(
        "/identity/password",
        headers=account["headers"],
        json={"current_password": "not-the-password", "new_password": "a-brand-new-passphrase"},
    )

    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_changing_a_password_ends_every_session_including_this_one(client, logged_in):
    """Otherwise changing your password after a compromise leaves the attacker in."""
    account = logged_in("rotate", password=SECRET)
    second = client.post(
        "/identity/login", json={"email": account["email"], "password": SECRET}
    ).json()
    other = {"Authorization": f"Bearer {second['token']}"}

    changed = client.post(
        "/identity/password",
        headers=account["headers"],
        json={"current_password": SECRET, "new_password": "a-brand-new-passphrase"},
    )

    assert changed.status_code == 204
    assert client.get("/identity/me", headers=account["headers"]).status_code == 401
    assert client.get("/identity/me", headers=other).status_code == 401

    signed_in = client.post(
        "/identity/login",
        json={"email": account["email"], "password": "a-brand-new-passphrase"},
    )
    assert signed_in.status_code == 200


def test_the_old_password_stops_working_after_a_change(client, logged_in):
    account = logged_in("old", password=SECRET)
    client.post(
        "/identity/password",
        headers=account["headers"],
        json={"current_password": SECRET, "new_password": "a-brand-new-passphrase"},
    )

    assert (
        client.post(
            "/identity/login", json={"email": account["email"], "password": SECRET}
        ).status_code
        == 401
    )


def test_a_hash_made_under_weaker_parameters_is_flagged_for_rehash(client):
    """Raising a cost leaves existing hashes at the old one. `needs_rehash` sees it."""
    weak = IdentitySettings(
        argon2_memory_kib=type(IdentitySettings().argon2_memory_kib)(
            value=8192.0, kind="security", rationale="test"
        )
    )
    old = hash_password(GOOD_PASSWORD, settings=weak)

    assert needs_rehash(old) is True
    assert needs_rehash(hash_password(GOOD_PASSWORD)) is False
