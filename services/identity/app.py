"""The Identity HTTP surface. Routing and translation only.

Every route reads a request, calls one function in `accounts`, `mfa` or
`sessions`, and returns what it gives back. Nothing here hashes, verifies
or decides — those all live in files that can be tested without a client.

## The dependency this service has that no other service has

`current_session_user` verifies a Bearer token and returns the user. It
is deliberately *not* Module 19's `current_user_id`: this service must
never accept the `X-Argus-User` stub, even on a deployment where the stub
is enabled. Changing your own password or enrolling your own second
factor through an authentication bypass would make the bypass a full
account takeover rather than a development convenience.

So the seam runs in one direction only — Modules 19/20/21 delegate their
identity to Module 22, and Module 22 delegates nothing back. A structural
test asserts no file in this package imports Module 19's stub.

## What a client sends

`Authorization: Bearer <token>`, from `POST /identity/login`. Declared as
a security scheme on the app so it appears in the generated docs, which
is the one thing reading the header off `request` in Modules 19 and 21
cost — worth naming rather than leaving as a quiet gap.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from infra.security.client_ip import resolve_client_ip
from infra.security.config import SecurityConfig
from infra.security.middleware import harden
from services.identity import accounts, mfa, sessions
from services.identity.config import IdentityConfig
from services.identity.errors import (
    IDENTITY_REQUIRED,
    SESSION_INVALID,
    IdentityError,
)
from services.identity.roles import ADMIN, require_role
from services.identity.schemas import (
    AccountResponse,
    ChangePasswordRequest,
    EnrolmentResponse,
    LoginRequest,
    MfaVerifyRequest,
    RegisterRequest,
    SessionResponse,
    SessionSummary,
)
from services.identity.tokens import bearer_token

__all__ = ["create_app", "current_session_user", "get_connection"]


def create_app(
    engine: Engine,
    config: IdentityConfig | None = None,
    security: SecurityConfig | None = None,
) -> FastAPI:
    settings = config or IdentityConfig()

    app = FastAPI(
        title="ARGUS Identity API",
        version="1",
        summary="Registration, sign-in, sessions, roles and second factors.",
        description=(
            "The only service in ARGUS that issues credentials. Every other service "
            "gets identity from here through one function — see "
            "services/terminal/identity.py."
        ),
    )
    app.state.engine = engine
    app.state.config = settings

    @app.exception_handler(IdentityError)
    async def _error(_request: Request, error: IdentityError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content=error.payload())

    app.include_router(_auth_router())
    app.include_router(_account_router())
    app.include_router(_mfa_router())
    app.include_router(_admin_router())
    return harden(app, security=security)


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def get_connection(request: Request) -> Iterator[Connection]:
    """A transaction that commits on a refusal as well as on a success.

    This is the one place in ARGUS where a handled error must not roll
    back, and getting it wrong silently disables the lockout.

    A failed login writes two rows — the `login_attempts` record the
    lockout counts, and the `audit_log` entry an operator reads — and
    then raises `IdentityError` to produce a 401. Under the ordinary
    `engine.begin()` pattern that exception aborts the transaction and
    takes both rows with it. The result is a brute-force protection whose
    evidence is destroyed by the refusal that generates it: every attempt
    counts as the first one, and the account never locks.

    So an `IdentityError` commits. It is a decided outcome, not a crash —
    the request was processed correctly and the answer is "no" — and its
    durable effects are the whole point. Every unexpected exception still
    rolls back.

    This is only safe because no path raises `IdentityError` after a
    partial write it would not want kept: registration validates the
    password and the address before inserting, a password change verifies
    before updating, and enrolment refuses before touching the row. A
    future path that writes and then refuses would need to say so.
    """
    engine: Engine = request.app.state.engine
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        except IdentityError:
            transaction.commit()
            raise
        except Exception:
            transaction.rollback()
            raise
        else:
            transaction.commit()


def get_config(request: Request) -> IdentityConfig:
    return request.app.state.config


ConnectionDep = Annotated[Connection, Depends(get_connection)]
ConfigDep = Annotated[IdentityConfig, Depends(get_config)]


def current_token(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> str:
    """The Bearer token, or a 401. Never the stub header — see the module docstring."""
    token = bearer_token(authorization)
    if token is None:
        raise IdentityError(
            IDENTITY_REQUIRED,
            "This endpoint needs a session. Send 'Authorization: Bearer <token>'.",
            status=401,
            detail={"scheme": "Bearer"},
        )
    return token


TokenDep = Annotated[str, Depends(current_token)]


def current_session_user(connection: ConnectionDep, token: TokenDep) -> UUID:
    """The user this session names, or a 401.

    Expired, revoked, forged and belonging-to-a-deactivated-account all
    produce the same refusal. The client's next action is identical in
    every case, and distinguishing them would let somebody probe which
    tokens once existed.
    """
    user_id = sessions.verify(connection, token)
    if user_id is None:
        raise IdentityError(
            SESSION_INVALID,
            "This session is not valid. Sign in again.",
            status=401,
        )
    return user_id


UserDep = Annotated[UUID, Depends(current_session_user)]


def client_address(request: Request) -> str | None:
    """The source address, for the attempt log and the per-source lockouts.

    Module 22 read `request.client` and deliberately not
    `X-Forwarded-For`, because behind no proxy that header is
    attacker-controlled. Module 24 closes the other half of that: behind
    a *real* proxy, refusing the header entirely means every request
    arrives from the proxy's one address, and the per-source lockout
    becomes a global one that a single attacker can trip to lock out
    everyone behind that proxy.

    `resolve_client_ip` is the fix — trust the header only from a
    TCP peer this deployment has explicitly configured as a proxy, and
    fall back to `request.client.host` (Module 22's original behaviour)
    for everyone else, including an unconfigured deployment. See
    `infra/security/client_ip.py`.
    """
    return resolve_client_ip(request, request.app.state.security.settings)


AddressDep = Annotated[str | None, Depends(client_address)]
AgentDep = Annotated[str | None, Header(alias="User-Agent")]


# --------------------------------------------------------------------------
# Registration and sign-in
# --------------------------------------------------------------------------


def _auth_router() -> APIRouter:
    router = APIRouter(prefix="/identity", tags=["authentication"])

    @router.post("/register", response_model=AccountResponse, status_code=201)
    def register(
        body: RegisterRequest,
        connection: ConnectionDep,
        config: ConfigDep,
        address: AddressDep,
    ) -> AccountResponse:
        """Create an account. Does not sign you in — that is a separate act."""
        account = accounts.register(
            connection,
            email=body.email,
            password=body.password,
            display_name=body.display_name,
            settings=config.settings,
            ip_address=address,
        )
        return AccountResponse(**asdict(account))

    @router.post("/login", response_model=SessionResponse)
    def login(
        body: LoginRequest,
        connection: ConnectionDep,
        config: ConfigDep,
        address: AddressDep,
        user_agent: AgentDep = None,
    ) -> SessionResponse:
        """Sign in. Returns the only copy of the session token that exists."""
        issued = accounts.log_in(
            connection,
            email=body.email,
            password=body.password,
            mfa_code=body.mfa_code,
            settings=config.settings,
            ip_address=address,
            user_agent=user_agent,
        )
        return SessionResponse(
            user_id=issued.user_id,
            session_id=issued.session_id,
            token=issued.token,
            issued_at=issued.issued_at,
            expires_at=issued.expires_at,
        )

    @router.post("/logout", status_code=204)
    def logout(
        connection: ConnectionDep,
        token: TokenDep,
        address: AddressDep,
    ) -> None:
        """End this session. Idempotent — signing out twice is not an error."""
        accounts.log_out(connection, token, ip_address=address)

    return router


# --------------------------------------------------------------------------
# Your own account
# --------------------------------------------------------------------------


def _account_router() -> APIRouter:
    router = APIRouter(prefix="/identity", tags=["account"])

    @router.get("/me", response_model=AccountResponse)
    def me(connection: ConnectionDep, user_id: UserDep) -> AccountResponse:
        return AccountResponse(**asdict(accounts.account_for(connection, user_id)))

    @router.get("/sessions", response_model=list[SessionSummary])
    def my_sessions(
        connection: ConnectionDep, config: ConfigDep, user_id: UserDep
    ) -> list[SessionSummary]:
        """Your own live sessions. Carries no tokens — there is nothing to leak."""
        return [
            SessionSummary(
                session_id=record.session_id,
                issued_at=record.issued_at,
                expires_at=record.expires_at,
                revoked_at=record.revoked_at,
                ip_address=record.ip_address,
                user_agent=record.user_agent,
                active=record.active(),
            )
            for record in sessions.list_for_user(
                connection, user_id, limit=int(config.settings.max_sessions_listed)
            )
        ]

    @router.post("/password", status_code=204)
    def change_password(
        body: ChangePasswordRequest,
        connection: ConnectionDep,
        config: ConfigDep,
        user_id: UserDep,
        address: AddressDep,
    ) -> None:
        """Replace your password. Ends every session, including this one."""
        accounts.change_password(
            connection,
            user_id,
            current_password=body.current_password,
            new_password=body.new_password,
            settings=config.settings,
            ip_address=address,
        )

    return router


# --------------------------------------------------------------------------
# Second factor
# --------------------------------------------------------------------------


def _mfa_router() -> APIRouter:
    router = APIRouter(prefix="/identity/mfa", tags=["mfa"])

    @router.post("/enroll", response_model=EnrolmentResponse)
    def enroll(
        connection: ConnectionDep,
        config: ConfigDep,
        user_id: UserDep,
    ) -> EnrolmentResponse:
        """Start enrolment. MFA is not on until a code is verified."""
        account = accounts.account_for(connection, user_id)
        enrolment = mfa.begin_enrolment(
            connection, user_id, email=account.email, settings=config.settings
        )
        return EnrolmentResponse(
            secret=enrolment.secret, provisioning_uri=enrolment.provisioning_uri
        )

    @router.post("/verify", response_model=AccountResponse)
    def verify(
        body: MfaVerifyRequest,
        connection: ConnectionDep,
        config: ConfigDep,
        user_id: UserDep,
        address: AddressDep,
    ) -> AccountResponse:
        """Finish enrolment by proving the authenticator works."""
        from services.identity import audit

        if not mfa.complete_enrolment(connection, user_id, body.code, settings=config.settings):
            raise IdentityError(
                "MFA_CODE_INVALID",
                "That code did not verify. Check your authenticator's clock and retry.",
                status=401,
            )
        audit.record(
            connection,
            audit.MFA_ENROLLED,
            actor_user_id=user_id,
            entity_id=user_id,
            payload={},
            ip_address=address,
        )
        return AccountResponse(**asdict(accounts.account_for(connection, user_id)))

    @router.delete("/enroll", response_model=AccountResponse)
    def unenroll(
        connection: ConnectionDep,
        user_id: UserDep,
        address: AddressDep,
    ) -> AccountResponse:
        """Remove your second factor. Audited, and clears the secret."""
        from services.identity import audit

        mfa.disable(connection, user_id)
        audit.record(
            connection,
            audit.MFA_DISABLED,
            actor_user_id=user_id,
            entity_id=user_id,
            payload={},
            ip_address=address,
        )
        return AccountResponse(**asdict(accounts.account_for(connection, user_id)))

    return router


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------


def _admin_router() -> APIRouter:
    """The one admin-gated surface, establishing the pattern.

    Deliberately small. Nothing in ARGUS currently needs admin gating
    beyond this module's own reach, and building an administration
    console would be the comprehensive admin panel the brief rules out.
    What exists is one route that demonstrates `require_role`, so the next
    module needing a gate has a shape to copy rather than a decision to
    re-make.
    """
    router = APIRouter(prefix="/identity/admin", tags=["admin"])

    @router.get("/users/{user_id}", response_model=AccountResponse)
    def read_user(
        user_id: UUID,
        connection: ConnectionDep,
        actor_id: UserDep,
    ) -> AccountResponse:
        """Read any account. Requires `admin`, and an enrolled second factor."""
        require_role(connection, actor_id, ADMIN)
        return AccountResponse(**asdict(accounts.account_for(connection, user_id)))

    @router.post("/users/{user_id}/deactivate", status_code=204)
    def deactivate(
        user_id: UUID,
        connection: ConnectionDep,
        actor_id: UserDep,
        address: AddressDep,
    ) -> None:
        """Disable an account and end its live sessions."""
        require_role(connection, actor_id, ADMIN)
        accounts.deactivate(connection, user_id, actor_user_id=actor_id, ip_address=address)

    return router
