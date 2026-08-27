"""The seam: that it held, and what `stub_identity_enabled` means now.

The claim in Module 19's report was that Module 22 would replace one
function body and touch nothing else. These tests check the parts of that
claim which are checkable — the signature, the call sites, and the
behaviour under each combination of credential and flag.
"""

from __future__ import annotations

import inspect
import logging
import secrets

import pytest
from fastapi.testclient import TestClient

from services.terminal.app import create_app as create_terminal
from services.terminal.app import get_connection as terminal_connection
from services.terminal.config import TerminalConfig
from services.terminal.identity import USER_HEADER, current_user_id


@pytest.fixture
def terminal_for(connection):
    """Module 19's app under a chosen config, over this test's connection."""

    def _app(config: TerminalConfig | None = None) -> TestClient:
        app = create_terminal(engine=None, config=config or TerminalConfig())  # type: ignore[arg-type]

        def _connection():
            savepoint = connection.begin_nested()
            try:
                yield connection
                savepoint.commit()
            except Exception:
                savepoint.rollback()
                raise

        app.dependency_overrides[terminal_connection] = _connection
        return TestClient(app, raise_server_exceptions=False)

    return _app


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_the_seams_signature_is_backward_compatible(client):
    """Module 19-era callers still compile: the new parameter has a default."""
    parameters = inspect.signature(current_user_id).parameters

    assert list(parameters)[:2] == ["connection", "raw_header"]
    assert parameters["config"].default is None
    assert parameters["authorization"].default is None
    assert parameters["authorization"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_seam_returns_a_uuid_naming_a_users_row(connection, logged_in):
    """The contract, literally: a UUID naming a row in `users`, or a TerminalError."""
    from uuid import UUID

    from sqlalchemy import select

    from infra.db.schema.users import users

    account = logged_in("contract")

    resolved = current_user_id(connection, None, authorization=f"Bearer {account['token']}")

    assert isinstance(resolved, UUID)
    assert (
        connection.execute(select(users.c.id).where(users.c.id == resolved)).scalar_one()
        == account["user_id"]
    )


def test_the_seam_raises_terminal_error_and_not_an_identity_error(connection):
    """Module 19's handler catches `TerminalError`. Raising anything else is a 500."""
    from services.terminal.errors import TerminalError

    with pytest.raises(TerminalError):
        current_user_id(connection, None, authorization="Bearer nonsense")


# --------------------------------------------------------------------------
# The flag's new meaning
# --------------------------------------------------------------------------


def test_a_session_works_with_the_stub_switched_off(terminal_for, logged_in):
    """The heart of the transition: real auth is never gated by a dev flag.

    Under Module 19's meaning this request would have been a 501. Under
    the new meaning the flag governs the bypass, so a properly
    authenticated caller is served on a deployment with the stub disabled
    — which is the deployment ARGUS is meant to run.
    """
    alice = logged_in("prod")
    closed = terminal_for(TerminalConfig(stub_identity_enabled=False))

    response = closed.get("/terminal/watchlists", headers=alice["headers"])

    assert response.status_code == 200


def test_the_header_stops_working_with_the_stub_switched_off(terminal_for, logged_in):
    """The bypass closes, and says so with the status Module 19 chose."""
    alice = logged_in("bypass")
    closed = terminal_for(TerminalConfig(stub_identity_enabled=False))

    response = closed.get("/terminal/watchlists", headers={USER_HEADER: str(alice["user_id"])})

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "IDENTITY_UNAVAILABLE"


def test_the_header_still_works_with_the_stub_on(terminal_for, logged_in):
    """Which is why Modules 19-21's suites pass unmodified."""
    alice = logged_in("dev")
    open_app = terminal_for(TerminalConfig(stub_identity_enabled=True))

    response = open_app.get("/terminal/watchlists", headers={USER_HEADER: str(alice["user_id"])})

    assert response.status_code == 200


def test_serving_a_request_through_the_stub_logs_a_warning(terminal_for, logged_in, caplog):
    """A bypass that runs silently is a bypass nobody notices in production."""
    alice = logged_in("noisy")
    open_app = terminal_for(TerminalConfig(stub_identity_enabled=True))

    with caplog.at_level(logging.WARNING, logger="argus.identity.seam"):
        open_app.get("/terminal/watchlists", headers={USER_HEADER: str(alice["user_id"])})

    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert warnings, "the stub path emitted no warning"
    assert "AUTHENTICATION BYPASS" in warnings[0].getMessage()
    assert str(alice["user_id"]) in warnings[0].getMessage()


def test_serving_a_request_through_a_session_logs_no_warning(terminal_for, logged_in, caplog):
    """Real authentication is the normal case and must not be noisy."""
    alice = logged_in("quiet")
    open_app = terminal_for(TerminalConfig(stub_identity_enabled=True))

    with caplog.at_level(logging.WARNING, logger="argus.identity.seam"):
        open_app.get("/terminal/watchlists", headers=alice["headers"])

    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


def test_an_invalid_token_is_not_downgraded_to_the_stub(terminal_for, logged_in):
    """A privilege escalation dressed as a fallback, refused.

    A request carrying an expired session *and* a header naming somebody
    else must not be served as that somebody. Falling through would mean
    an attacker with any expired token could pick a user id.
    """
    alice = logged_in("victim")
    mallory = logged_in("attacker")
    open_app = terminal_for(TerminalConfig(stub_identity_enabled=True))

    response = open_app.get(
        "/terminal/watchlists",
        headers={
            "Authorization": f"Bearer {secrets.token_urlsafe(32)}",
            USER_HEADER: str(alice["user_id"]),
        },
    )

    assert response.status_code == 401
    assert mallory["user_id"] != alice["user_id"]


def test_a_session_beats_a_header_naming_somebody_else(terminal_for, pair, terminal):
    """When both are present and both are valid, the session wins."""
    alice, mallory = pair
    open_app = terminal_for(TerminalConfig(stub_identity_enabled=True))

    response = open_app.get(
        "/terminal/watchlists",
        headers={**mallory["headers"], USER_HEADER: str(alice["user_id"])},
    )

    assert response.status_code == 200
    assert response.json() == [], "Mallory's own empty list, not Alice's"


def test_no_credential_at_all_with_the_stub_off_is_the_501(terminal_for):
    """The one request that genuinely cannot be answered."""
    closed = terminal_for(TerminalConfig(stub_identity_enabled=False))

    response = closed.get("/terminal/watchlists")

    assert response.status_code == 501
    assert "Module 22" in response.json()["error"]["message"]


def test_no_credential_at_all_with_the_stub_on_is_a_401(terminal_for):
    """The stub exists but was not used; that is the caller's omission."""
    open_app = terminal_for(TerminalConfig(stub_identity_enabled=True))

    response = open_app.get("/terminal/watchlists")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "IDENTITY_REQUIRED"


def test_public_endpoints_are_unaffected_by_any_of_this(terminal_for, register):
    """Company data is not user-scoped and must not be collateral damage."""
    register("SEAM")
    closed = terminal_for(TerminalConfig(stub_identity_enabled=False))

    assert closed.get("/terminal/companies/SEAM/fundamentals").status_code == 200


# --------------------------------------------------------------------------
# Module 21 through the same seam
# --------------------------------------------------------------------------


def test_module_21_resolves_a_session_through_the_same_function(connection, logged_in):
    """One identity mechanism, not two — which was Module 21's stated reason.

    Checked by behaviour rather than by reading the source: a real
    session token resolves to the same user through Module 21's
    dependency as through Module 19's, because both call one function.
    """
    from services.intelligence.app import optional_user

    account = logged_in("intel")

    class _Request:
        headers = {"Authorization": f"Bearer {account['token']}"}

    resolved = optional_user(_Request(), connection, None)  # type: ignore[arg-type]

    assert resolved == account["user_id"]
    assert current_user_id(connection, None, authorization=f"Bearer {account['token']}") == resolved


def test_module_21_treats_an_invalid_credential_as_anonymous(connection, logged_in):
    """Nothing there is user-scoped yet, so a bad token means anonymous, not refused."""
    from fastapi.testclient import TestClient

    from services.intelligence.app import create_app, get_connection

    app = create_app(engine=None)  # type: ignore[arg-type]

    def _connection():
        savepoint = connection.begin_nested()
        try:
            yield connection
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise

    app.dependency_overrides[get_connection] = _connection
    with TestClient(app, raise_server_exceptions=False) as intelligence:
        response = intelligence.get(
            "/intelligence/watchlists/CONSOLIDATION",
            headers={"Authorization": "Bearer nonsense"},
        )

    assert response.status_code == 200
