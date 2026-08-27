"""Brute-force protection, exercised rather than described.

Every test here drives real failed logins through the API and then checks
what the next one does. The lockout is counted from an append-only table,
so these also prove the count survives the thing an attacker would try
next — clearing it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, InternalError

from infra.db.schema.users import login_attempts
from services.identity import attempts
from services.identity.config import IdentitySettings
from tests.integration.identity.conftest import GOOD_PASSWORD

LIMIT = int(IdentitySettings().max_failed_attempts)


def _fail_login(client, address: str, times: int = 1) -> list[int]:
    return [
        client.post(
            "/identity/login", json={"email": address, "password": "definitely-wrong"}
        ).status_code
        for _ in range(times)
    ]


def test_repeated_failures_lock_the_account(client, signed_up):
    """Five wrong passwords, then the sixth attempt is refused before hashing."""
    account = signed_up("locked")

    statuses = _fail_login(client, account["email"], LIMIT)
    assert statuses == [401] * LIMIT, "each failure is an ordinary refusal"

    locked = client.post(
        "/identity/login", json={"email": account["email"], "password": "wrong-again"}
    )
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "ACCOUNT_LOCKED"


def test_the_lock_holds_against_the_correct_password(client, signed_up):
    """The point of a lockout: guessing right at attempt six does not help.

    If the correct password were accepted while locked, the lockout would
    only be slowing an attacker down by the round trips they were making
    anyway.
    """
    account = signed_up("holds")
    _fail_login(client, account["email"], LIMIT)

    correct = client.post(
        "/identity/login", json={"email": account["email"], "password": GOOD_PASSWORD}
    )

    assert correct.status_code == 429
    assert correct.json()["error"]["code"] == "ACCOUNT_LOCKED"


def test_the_lock_names_when_it_lifts(client, signed_up):
    """Withholding this protects nothing and confuses the person who is locked out."""
    account = signed_up("retry")
    _fail_login(client, account["email"], LIMIT)

    locked = client.post("/identity/login", json={"email": account["email"], "password": "wrong"})

    remaining = locked.json()["error"]["detail"]["retry_after_seconds"]
    assert 0 < remaining <= IdentitySettings().lockout_seconds


def test_four_failures_do_not_lock(client, signed_up):
    """The threshold is a threshold, not a hair trigger — people mistype."""
    account = signed_up("tolerant")
    _fail_login(client, account["email"], LIMIT - 1)

    correct = client.post(
        "/identity/login", json={"email": account["email"], "password": GOOD_PASSWORD}
    )

    assert correct.status_code == 200


def test_a_success_clears_the_count_without_erasing_the_history(client, connection, signed_up):
    """ "Failures since the last success" is what replaces a counter here.

    The count resets; the failures stay on the record. A reset counter
    would have thrown away the evidence that the account was attacked.
    """
    account = signed_up("clears")
    _fail_login(client, account["email"], LIMIT - 1)

    assert (
        client.post(
            "/identity/login", json={"email": account["email"], "password": GOOD_PASSWORD}
        ).status_code
        == 200
    )

    # Four more would have locked it, had the count not reset.
    assert _fail_login(client, account["email"], LIMIT - 1) == [401] * (LIMIT - 1)

    recorded = connection.execute(
        select(func.count())
        .select_from(login_attempts)
        .where(login_attempts.c.email == account["email"])
    ).scalar_one()
    assert recorded == (LIMIT - 1) + 1 + (LIMIT - 1), "every attempt is still on record"


def test_failures_outside_the_window_do_not_count(client, connection, signed_up):
    """Five mistakes spread over a year must never lock anybody out."""
    account = signed_up("windowed")
    stale = datetime.now(UTC) - timedelta(seconds=IdentitySettings().lockout_window_seconds + 60)
    for _ in range(LIMIT + 2):
        attempts.record_attempt(
            connection,
            email=account["email"],
            succeeded=False,
            reason=attempts.BAD_PASSWORD,
            at=stale,
        )

    correct = client.post(
        "/identity/login", json={"email": account["email"], "password": GOOD_PASSWORD}
    )

    assert correct.status_code == 200


def test_the_lockout_is_case_insensitive_on_the_address(client, signed_up):
    """Otherwise a lockout is evaded by pressing shift."""
    account = signed_up("cased")
    _fail_login(client, account["email"].upper(), LIMIT)

    locked = client.post(
        "/identity/login", json={"email": account["email"], "password": GOOD_PASSWORD}
    )

    assert locked.status_code == 429


def test_locking_one_account_does_not_lock_another(client, signed_up):
    """A lockout that spread across accounts would be a denial of service."""
    victim = signed_up("victim")
    bystander = signed_up("bystander")
    _fail_login(client, victim["email"], LIMIT)

    other = client.post(
        "/identity/login", json={"email": bystander["email"], "password": GOOD_PASSWORD}
    )

    assert other.status_code == 200


def test_guessing_an_address_that_does_not_exist_is_also_rate_limited(client, email):
    """Otherwise enumeration is free — and the response must not reveal that either."""
    ghost = email("ghost")

    statuses = _fail_login(client, ghost, LIMIT)
    locked = client.post("/identity/login", json={"email": ghost, "password": "wrong"})

    assert statuses == [401] * LIMIT
    assert locked.status_code == 429


def test_a_spray_across_many_accounts_trips_the_per_source_limit(client, connection, signed_up):
    """The attack per-account lockouts are famously blind to.

    One password against ten thousand addresses never gives any single
    account more than one failure. The per-source limit is what sees it.
    """
    target = signed_up("sprayed")
    per_address = int(IdentitySettings().max_failed_attempts_per_address)

    for index in range(per_address):
        attempts.record_attempt(
            connection,
            email=f"victim-{index}@argus.test",
            succeeded=False,
            reason=attempts.BAD_PASSWORD,
            ip_address="testclient",
        )

    # This account has zero failures of its own and is still refused,
    # because the source address has been spraying.
    blocked = client.post(
        "/identity/login", json={"email": target["email"], "password": GOOD_PASSWORD}
    )

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "ACCOUNT_LOCKED"


def test_the_per_source_limit_is_looser_than_the_per_account_one(client):
    """A shared office NAT genuinely produces more failures than one person does."""
    settings = IdentitySettings()

    assert int(settings.max_failed_attempts_per_address) > int(settings.max_failed_attempts)


def test_the_attempt_log_cannot_be_deleted_to_clear_a_lockout(client, connection, signed_up):
    """A lockout you can DELETE is not a lockout. The guard is in the database."""
    account = signed_up("undeletable")
    _fail_login(client, account["email"], LIMIT)

    savepoint = connection.begin_nested()
    with pytest.raises((IntegrityError, InternalError)) as raised:
        connection.execute(
            login_attempts.delete().where(login_attempts.c.email == account["email"])
        )
    assert isinstance(raised.value.orig, psycopg.errors.RestrictViolation)
    savepoint.rollback()


def test_the_attempt_log_cannot_be_rewritten_either(client, connection, signed_up):
    """Flipping `succeeded` would clear the count just as effectively as a delete."""
    account = signed_up("unupdatable")
    _fail_login(client, account["email"], 1)

    savepoint = connection.begin_nested()
    with pytest.raises((IntegrityError, InternalError)) as raised:
        connection.execute(
            login_attempts.update()
            .where(login_attempts.c.email == account["email"])
            .values(succeeded=True)
        )
    assert isinstance(raised.value.orig, psycopg.errors.RestrictViolation)
    savepoint.rollback()


def test_the_attempt_log_records_the_reason_and_never_the_credential(client, connection, signed_up):
    account = signed_up("reasons")
    _fail_login(client, account["email"], 1)

    row = connection.execute(
        select(login_attempts).where(login_attempts.c.email == account["email"])
    ).one()

    assert row.succeeded is False
    assert row.reason == attempts.BAD_PASSWORD
    assert "definitely-wrong" not in str(row)


def test_an_attempt_against_an_unknown_address_records_no_user_but_keeps_the_address(
    client, connection, email
):
    """Dropping the rows that matched nothing would hide the enumeration."""
    ghost = email("nobody")
    _fail_login(client, ghost, 1)

    row = connection.execute(select(login_attempts).where(login_attempts.c.email == ghost)).one()

    assert row.user_id is None
    assert row.email == ghost
    assert row.reason == attempts.NO_SUCH_USER


def test_hammering_a_locked_account_keeps_extending_the_record(client, connection, signed_up):
    """The lock must not make the attack invisible the moment it engages."""
    account = signed_up("hammered")
    _fail_login(client, account["email"], LIMIT)
    before = _count(connection, account["email"])

    _fail_login(client, account["email"], 3)

    assert _count(connection, account["email"]) == before + 3


def _count(connection, address: str) -> int:
    return connection.execute(
        select(func.count()).select_from(login_attempts).where(login_attempts.c.email == address)
    ).scalar_one()


# --------------------------------------------------------------------------
# The dependency that decides whether any of the above survives
# --------------------------------------------------------------------------


def test_the_real_dependency_commits_the_evidence_of_a_refusal(engine):
    """The shipped `get_connection`, not the fixture that mimics it.

    Every other test in this file runs against a fixture that overrides
    `get_connection`, so none of them can see whether the real dependency
    rolls a refusal back. A break attempt that removed the commit-on-
    `IdentityError` branch passed this whole file — which is exactly the
    shape of the bug the branch exists to fix, and the reason this test
    reaches past the fixture to the thing that actually ships.
    """
    from types import SimpleNamespace

    from services.identity.app import get_connection
    from services.identity.errors import IdentityError

    address = f"real-dependency-{uuid4().hex[:12]}@argus.test"
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))

    generator = get_connection(request)
    conn = next(generator)
    attempts.record_attempt(conn, email=address, succeeded=False, reason=attempts.BAD_PASSWORD)
    with pytest.raises(IdentityError):
        generator.throw(IdentityError("TEST", "a refusal"))

    with engine.connect() as fresh:
        surviving = fresh.execute(
            select(func.count())
            .select_from(login_attempts)
            .where(login_attempts.c.email == address)
        ).scalar_one()

    assert surviving == 1, "the attempt a refusal recorded must outlive the refusal"


def test_the_real_dependency_still_rolls_back_an_unexpected_error(engine):
    """Committing on a refusal must not become committing on a crash."""
    from types import SimpleNamespace

    from services.identity.app import get_connection

    address = f"crash-{uuid4().hex[:12]}@argus.test"
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))

    generator = get_connection(request)
    conn = next(generator)
    attempts.record_attempt(conn, email=address, succeeded=False, reason=attempts.BAD_PASSWORD)
    with pytest.raises(RuntimeError):
        generator.throw(RuntimeError("something unexpected"))

    with engine.connect() as fresh:
        surviving = fresh.execute(
            select(func.count())
            .select_from(login_attempts)
            .where(login_attempts.c.email == address)
        ).scalar_one()

    assert surviving == 0
