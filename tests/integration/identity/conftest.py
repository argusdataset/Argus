"""Fixtures for Module 22.

Everything here runs against real PostgreSQL and the real argon2 hasher.
A faked hash would make the timing test meaningless, a faked session
table would make the expiry test meaningless, and the append-only guard
on `login_attempts` — which is what makes the lockout unclearable — only
exists in the database.

The argon2 parameters are the real ones. That costs about 25ms per
password operation and roughly a second across this suite, which is the
correct price for testing the thing that ships rather than a fast
stand-in for it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.engine import Connection

from infra.db.schema.users import users
from services.identity.app import create_app, get_connection
from services.identity.config import IdentityConfig
from services.identity.errors import IdentityError
from services.identity.roles import ADMIN, REGISTERED_USER, role_id_for
from services.terminal.app import create_app as create_terminal
from services.terminal.app import get_connection as terminal_connection
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)

#: A password that clears the length floor without being interesting.
GOOD_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def config() -> IdentityConfig:
    return IdentityConfig()


@pytest.fixture
def client(connection: Connection, config: IdentityConfig) -> Iterator[TestClient]:
    app = create_app(engine=None, config=config)  # type: ignore[arg-type]

    def _connection() -> Iterator[Connection]:
        """Mirrors `get_connection`, including the part that matters.

        An `IdentityError` releases the savepoint rather than rolling it
        back, exactly as the real dependency commits rather than rolling
        back — see `services/identity/app.py`. A fixture that rolled back
        on a refusal would make every lockout test pass while the shipped
        lockout did nothing.
        """
        savepoint = connection.begin_nested()
        try:
            yield connection
        except IdentityError:
            savepoint.commit()
            raise
        except Exception:
            savepoint.rollback()
            raise
        else:
            savepoint.commit()

    app.dependency_overrides[get_connection] = _connection
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def email() -> Callable[[str], str]:
    """A unique address per call. `users.email` is unique across the suite."""

    def _email(label: str = "user") -> str:
        return f"{label}-{uuid4().hex[:12]}@argus.test"

    return _email


@pytest.fixture
def signed_up(client: TestClient, email: Callable[[str], str]) -> Callable[..., dict]:
    """Register an account through the API and hand back its details.

    Through the API rather than by insert: registration is one of the
    paths under test, and a hand-built `users` row would let a hashing
    bug pass every login test in the file.
    """

    def _signup(
        label: str = "user",
        *,
        password: str = GOOD_PASSWORD,
        address: str | None = None,
    ) -> dict:
        address = address or email(label)
        response = client.post(
            "/identity/register",
            json={"email": address, "password": password, "display_name": label},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        # `user_id` comes back as a string over the wire and is compared
        # against database rows all over this suite. Converted once here
        # rather than at every call site.
        body["user_id"] = UUID(body["user_id"])
        return {"email": address, "password": password, **body}

    return _signup


@pytest.fixture
def logged_in(client: TestClient, signed_up: Callable[..., dict]) -> Callable[..., dict]:
    """Register and sign in. Returns the account plus its bearer headers."""

    def _login(label: str = "user", **kwargs) -> dict:
        account = signed_up(label, **kwargs)
        response = client.post(
            "/identity/login",
            json={"email": account["email"], "password": account["password"]},
        )
        assert response.status_code == 200, response.text
        session = response.json()
        return {
            **account,
            "token": session["token"],
            "session_id": session["session_id"],
            "headers": {"Authorization": f"Bearer {session['token']}"},
        }

    return _login


@pytest.fixture
def promote(connection: Connection) -> Callable[[UUID, str], None]:
    """Change a user's role directly.

    Directly rather than through an endpoint because no endpoint grants
    roles — see the module report on why role assignment was left out of
    an API in this module.
    """

    def _promote(user_id: UUID, role: str = ADMIN) -> None:
        connection.execute(
            users.update()
            .where(users.c.id == user_id)
            .values(role_id=role_id_for(connection, role))
        )

    return _promote


@pytest.fixture
def enrol_mfa(client: TestClient, connection: Connection) -> Callable[..., str]:
    """Take an account through TOTP enrolment. Returns the secret."""
    import pyotp

    def _enrol(headers: dict[str, str], *, now: datetime | None = None) -> str:
        started = client.post("/identity/mfa/enroll", headers=headers)
        assert started.status_code == 200, started.text
        secret = started.json()["secret"]

        code = pyotp.TOTP(secret).at(now or datetime.now(UTC))
        verified = client.post("/identity/mfa/verify", headers=headers, json={"code": code})
        assert verified.status_code == 200, verified.text
        assert verified.json()["mfa_enabled"] is True
        return secret

    return _enrol


@pytest.fixture
def register(connection: Connection) -> Callable[..., UUID]:
    """A real security, for the watchlist-isolation sweep."""
    from data.canonical_model.exchanges import CanonicalExchange
    from data.normalization.identity import SecurityIdentityResolver

    resolver = SecurityIdentityResolver(connection)

    def _register(ticker: str) -> UUID:
        return resolver.register(
            ticker,
            exchange=CanonicalExchange.NASDAQ,
            valid_from=datetime(2000, 1, 1, tzinfo=UTC),
            name=f"{ticker} Test Corp.",
        )

    return _register


@pytest.fixture
def stored_hash(connection: Connection) -> Callable[[UUID], str | None]:
    def _hash(user_id: UUID) -> str | None:
        return connection.execute(
            select(users.c.password_hash).where(users.c.id == user_id)
        ).scalar_one_or_none()

    return _hash


@pytest.fixture
def terminal(connection):
    """Module 19's app, unmodified, served over the same database."""
    app = create_terminal(engine=None)  # type: ignore[arg-type]

    def _connection():
        savepoint = connection.begin_nested()
        try:
            yield connection
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise

    app.dependency_overrides[terminal_connection] = _connection
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def pair(logged_in, terminal):
    """Alice with a private watchlist, and Mallory who wants to see it."""
    alice = logged_in("alice")
    mallory = logged_in("mallory")

    created = terminal.post(
        "/terminal/watchlists",
        headers=alice["headers"],
        json={"name": "Alice's bases"},
    )
    assert created.status_code == 201, created.text
    alice["watchlist_id"] = created.json()["id"]
    return alice, mallory


@pytest.fixture
def terminal_for(connection: Connection):
    """Module 19's app under a chosen config, over this test's connection."""

    def _app(config=None) -> TestClient:
        from services.terminal.config import TerminalConfig

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


__all__ = ["ADMIN", "GOOD_PASSWORD", "REGISTERED_USER"]
