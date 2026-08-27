"""TOTP enrolment and verification, and the three-role access check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
from sqlalchemy import select

from infra.db.schema.users import users
from services.identity import mfa
from services.identity.config import IdentitySettings
from services.identity.roles import (
    ADMIN,
    PUBLIC,
    REGISTERED_USER,
    has_at_least,
    rank_of,
    require_role,
)
from tests.integration.identity.conftest import GOOD_PASSWORD

STEP = timedelta(seconds=int(IdentitySettings().totp_step_seconds))


def _next_code(secret: str) -> str:
    """The code for the step after this one.

    Needed because completing enrolment *spends* the counter it verified,
    so the code a person just typed cannot immediately be reused to sign
    in. That is the replay rule working as intended and is a real thing a
    client must know: after enrolling, wait for the next code. It is
    accepted before its step begins because the skew window is one step
    either side.
    """
    return pyotp.TOTP(secret).at(datetime.now(UTC) + STEP)


# --------------------------------------------------------------------------
# MFA
# --------------------------------------------------------------------------


def test_enrolment_and_verification_round_trip(client, logged_in, enrol_mfa):
    """Start enrolment, prove the authenticator works, MFA is on."""
    account = logged_in("mfa")
    assert account["mfa_enabled"] is False

    secret = enrol_mfa(account["headers"])

    assert secret
    assert client.get("/identity/me", headers=account["headers"]).json()["mfa_enabled"] is True


def test_enrolment_does_not_enable_mfa_until_a_code_is_verified(client, connection, logged_in):
    """One step would lock somebody out with a factor they had not tested yet."""
    account = logged_in("twostep")

    started = client.post("/identity/mfa/enroll", headers=account["headers"])

    assert started.status_code == 200
    assert started.json()["mfa_enabled"] is False
    row = connection.execute(select(users).where(users.c.id == account["user_id"])).one()
    assert row.mfa_secret is not None, "the secret is stored"
    assert row.mfa_enabled is False, "and MFA is still off"


def test_a_wrong_code_does_not_complete_enrolment(client, logged_in):
    account = logged_in("badcode")
    client.post("/identity/mfa/enroll", headers=account["headers"])

    response = client.post(
        "/identity/mfa/verify", headers=account["headers"], json={"code": "000000"}
    )

    assert response.status_code == 401
    assert client.get("/identity/me", headers=account["headers"]).json()["mfa_enabled"] is False


def test_login_requires_the_code_once_mfa_is_on(client, logged_in, enrol_mfa):
    """The password alone stops being enough."""
    account = logged_in("gated")
    secret = enrol_mfa(account["headers"])

    without = client.post(
        "/identity/login", json={"email": account["email"], "password": GOOD_PASSWORD}
    )
    assert without.status_code == 401
    assert without.json()["error"]["code"] == "MFA_CODE_REQUIRED"

    with_code = client.post(
        "/identity/login",
        json={
            "email": account["email"],
            "password": GOOD_PASSWORD,
            "mfa_code": _next_code(secret),
        },
    )
    assert with_code.status_code == 200
    assert with_code.json()["token"]


def test_a_wrong_code_at_login_is_refused_and_issues_no_session(client, logged_in, enrol_mfa):
    account = logged_in("wrongcode")
    enrol_mfa(account["headers"])

    response = client.post(
        "/identity/login",
        json={
            "email": account["email"],
            "password": GOOD_PASSWORD,
            "mfa_code": "123456",
        },
    )

    assert response.status_code == 401
    assert "token" not in response.json()


def test_a_code_cannot_be_used_twice(client, connection, logged_in, enrol_mfa):
    """Replay protection. A one-time password that works twice is not one.

    A code stays valid for its whole time step and, with the skew window,
    is accepted across ninety seconds. Without `mfa_last_counter` a code
    observed once works again inside that window.
    """
    account = logged_in("replay")
    secret = enrol_mfa(account["headers"])
    code = _next_code(secret)

    first = client.post(
        "/identity/login",
        json={"email": account["email"], "password": GOOD_PASSWORD, "mfa_code": code},
    )
    second = client.post(
        "/identity/login",
        json={"email": account["email"], "password": GOOD_PASSWORD, "mfa_code": code},
    )

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "MFA_CODE_REQUIRED"


def test_a_code_from_an_earlier_step_is_also_refused_after_a_later_one(
    client, connection, logged_in, enrol_mfa
):
    """The whole prefix is spent, not just the exact counter.

    Otherwise an attacker holding a slightly older code from the same
    skew window could still use it after the real user signed in.
    """
    account = logged_in("prefix")
    secret = enrol_mfa(account["headers"])
    now = datetime.now(UTC)
    later = pyotp.TOTP(secret).at(now + STEP)
    earlier = pyotp.TOTP(secret).at(now)

    assert mfa.verify_code(connection, account["user_id"], later, now=now) is True
    assert mfa.verify_code(connection, account["user_id"], earlier, now=now) is False


def test_the_counter_recorded_is_the_one_that_matched(client, connection, logged_in, enrol_mfa):
    account = logged_in("counter")
    secret = enrol_mfa(account["headers"])
    now = datetime.now(UTC)

    assert mfa.verify_code(
        connection, account["user_id"], pyotp.TOTP(secret).at(now + STEP), now=now
    )

    stored = connection.execute(
        select(users.c.mfa_last_counter).where(users.c.id == account["user_id"])
    ).scalar_one()
    assert stored == mfa.counter_for(now) + 1


def test_a_code_within_the_skew_window_is_accepted(client, connection, logged_in, enrol_mfa):
    """A phone a few seconds off must still work."""
    account = logged_in("skew")
    secret = enrol_mfa(account["headers"])
    now = datetime.now(UTC)

    # Fresh enrolment consumed the current step, so look forward rather
    # than back — the prefix rule correctly refuses anything older.
    ahead = pyotp.TOTP(secret).at(now + timedelta(seconds=30))

    assert mfa.verify_code(connection, account["user_id"], ahead, now=now) is True


def test_a_code_outside_the_skew_window_is_refused(client, connection, logged_in, enrol_mfa):
    account = logged_in("faroff")
    secret = enrol_mfa(account["headers"])
    now = datetime.now(UTC)
    far = pyotp.TOTP(secret).at(now + timedelta(minutes=10))

    assert mfa.verify_code(connection, account["user_id"], far, now=now) is False


def test_enrolling_again_over_a_working_factor_is_refused(client, logged_in, enrol_mfa):
    """Silently replacing a second factor would be a takeover for a stolen session."""
    account = logged_in("reenrol")
    enrol_mfa(account["headers"])

    again = client.post("/identity/mfa/enroll", headers=account["headers"])

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "MFA_ALREADY_ENROLLED"


def test_removing_a_factor_clears_the_secret_and_not_just_the_flag(
    client, connection, logged_in, enrol_mfa
):
    """A stale secret would be silently reused by a later re-enrolment."""
    account = logged_in("unenrol")
    enrol_mfa(account["headers"])

    removed = client.delete("/identity/mfa/enroll", headers=account["headers"])

    assert removed.status_code == 200
    assert removed.json()["mfa_enabled"] is False
    row = connection.execute(select(users).where(users.c.id == account["user_id"])).one()
    assert row.mfa_secret is None
    assert row.mfa_last_counter is None


def test_the_enrolment_secret_is_the_only_credential_this_service_returns(client, logged_in):
    """And it is returned once, at the moment there is no other way to send it."""
    account = logged_in("onlyone")

    body = client.post("/identity/mfa/enroll", headers=account["headers"]).json()

    assert set(body) == {"secret", "provisioning_uri", "mfa_enabled"}
    assert body["provisioning_uri"].startswith("otpauth://totp/")
    assert "ARGUS" in body["provisioning_uri"]


# --------------------------------------------------------------------------
# RBAC
# --------------------------------------------------------------------------


def test_the_three_roles_are_totally_ordered(client):
    """Which is why Module 03's single role_id column is still sufficient."""
    assert rank_of(PUBLIC) < rank_of(REGISTERED_USER) < rank_of(ADMIN)
    assert has_at_least(ADMIN, REGISTERED_USER)
    assert has_at_least(REGISTERED_USER, REGISTERED_USER)
    assert not has_at_least(REGISTERED_USER, ADMIN)
    assert not has_at_least(PUBLIC, REGISTERED_USER)


def test_the_three_roles_are_seeded_in_the_database(client, connection):
    """Migration 0012 seeds them: a deployment with an empty roles table registers nobody."""
    from services.identity.roles import role_id_for

    for name in (PUBLIC, REGISTERED_USER, ADMIN):
        assert role_id_for(connection, name) is not None


def test_a_registered_user_cannot_reach_an_admin_action(client, logged_in):
    """The gate, from the outside."""
    victim = logged_in("target")
    ordinary = logged_in("ordinary")

    response = client.get(f"/identity/admin/users/{victim['user_id']}", headers=ordinary["headers"])

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert response.json()["error"]["detail"]["required"] == ADMIN


def test_an_admin_with_a_second_factor_can(client, logged_in, promote, enrol_mfa):
    """The other half of the gate: it opens for the right person."""
    victim = logged_in("subject")
    admin = logged_in("root")
    promote(admin["user_id"], ADMIN)
    enrol_mfa(admin["headers"])

    response = client.get(f"/identity/admin/users/{victim['user_id']}", headers=admin["headers"])

    assert response.status_code == 200
    assert response.json()["user_id"] == str(victim["user_id"])


def test_an_admin_without_a_second_factor_is_refused_the_admin_surface(client, logged_in, promote):
    """The MFA requirement made real rather than advisory.

    Enforced at the gate rather than at login, deliberately: an admin who
    cannot sign in cannot enrol, which would make the requirement
    unsatisfiable.
    """
    victim = logged_in("subject2")
    admin = logged_in("bare")
    promote(admin["user_id"], ADMIN)

    response = client.get(f"/identity/admin/users/{victim['user_id']}", headers=admin["headers"])

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MFA_REQUIRED"


def test_that_admin_can_still_sign_in_and_enrol(client, logged_in, promote, enrol_mfa):
    """Which is what makes the requirement satisfiable."""
    admin = logged_in("bootstrap")
    promote(admin["user_id"], ADMIN)

    enrol_mfa(admin["headers"])

    assert client.get("/identity/me", headers=admin["headers"]).json()["mfa_enabled"] is True


def test_a_registered_user_passes_a_registered_user_gate(client, connection, logged_in):
    account = logged_in("ordinary2")

    assert require_role(connection, account["user_id"], REGISTERED_USER) == REGISTERED_USER


def test_mfa_is_not_required_of_an_ordinary_user(client, logged_in):
    """The documented scope decision, asserted: optional below admin.

    ARGUS has no account-recovery flow. Mandating a second factor for
    everyone in that state trades a certain lockout risk against a
    marginal gain on an account holding a personal list of tickers.
    """
    account = logged_in("optional")

    assert account["mfa_enabled"] is False
    assert client.get("/identity/me", headers=account["headers"]).status_code == 200


def test_the_admin_gate_also_checks_mfa_for_admin_only(client, connection, logged_in, promote):
    """`registered_user` gating never demands a second factor."""
    account = logged_in("nofactor")

    assert require_role(connection, account["user_id"], REGISTERED_USER) == REGISTERED_USER

    promote(account["user_id"], ADMIN)
    assert require_role(connection, account["user_id"], REGISTERED_USER) == ADMIN


def test_the_totp_step_is_structural_and_the_skew_is_a_defence(client):
    """The kinds are not decoration: one changes what a code is, one how strictly it is checked."""
    settings = IdentitySettings()

    assert settings.totp_step_seconds.kind == "structural"
    assert settings.totp_skew_steps.kind == "security"
    assert "totp_skew_steps" in settings.security()
    assert "totp_step_seconds" not in settings.security()


def test_completing_enrolment_spends_that_code(client, logged_in):
    """Documented because a client has to handle it: enrol, then wait one step.

    The code typed to finish enrolment is verified and recorded as spent,
    so signing in with the same code immediately afterwards is refused.
    Any other behaviour would mean a code that works twice, which is what
    the counter exists to prevent — the cost lands here, visibly, rather
    than being traded away.
    """
    account = logged_in("spends")
    started = client.post("/identity/mfa/enroll", headers=account["headers"])
    secret = started.json()["secret"]
    code = pyotp.TOTP(secret).now()

    assert (
        client.post(
            "/identity/mfa/verify", headers=account["headers"], json={"code": code}
        ).status_code
        == 200
    )

    reused = client.post(
        "/identity/login",
        json={"email": account["email"], "password": GOOD_PASSWORD, "mfa_code": code},
    )

    assert reused.status_code == 401
    assert reused.json()["error"]["code"] == "MFA_CODE_REQUIRED"
