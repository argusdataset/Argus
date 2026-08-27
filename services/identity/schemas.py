"""The Identity service's request and response shapes.

## The rule every model here follows

No response model has a field that could hold a credential. Not
`password_hash`, not `token_hash`, not `mfa_secret`. The one exception is
deliberate and narrow: `EnrolmentResponse.secret`, returned exactly once
at the moment a person is setting up an authenticator and has no other
way to receive it. Everything else about MFA is reported as a boolean.

That is not left to discipline. A structural test walks these models'
Pydantic `model_fields` — the same AST-and-metadata approach Modules 20
and 21 established, rather than grepping source text — and fails on any
credential-shaped field outside the one allowed place.

## `SessionResponse.token` is the other single-use value

A session token is returned once, by login, and is never recoverable
afterwards: `sessions` stores only its hash. `SessionSummary`, which is
what listing your own sessions returns, has no token field at all — so
there is no endpoint anywhere in ARGUS that can hand back a live
credential to somebody who did not just create it.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "AccountResponse",
    "ChangePasswordRequest",
    "EnrolmentResponse",
    "LoginRequest",
    "MfaVerifyRequest",
    "RegisterRequest",
    "SessionResponse",
    "SessionSummary",
]


class RegisterRequest(BaseModel):
    email: str = Field(description="The address this account signs in with.")
    password: str = Field(
        description="Minimum length is enforced server-side and nothing else is.",
    )
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str
    #: Required only for accounts with an enrolled second factor. Absent
    #: and wrong are the same answer, so a caller cannot use this field to
    #: discover whether an account has MFA.
    mfa_code: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(
        description=(
            "Required even though you hold a session. A session proves somebody "
            "signed in once; it does not prove the person here now knows the password."
        )
    )
    new_password: str


class MfaVerifyRequest(BaseModel):
    code: str = Field(description="Six digits from the authenticator app.")


class AccountResponse(BaseModel):
    """A user, as this service reports one. No credential material."""

    user_id: UUID
    email: str
    display_name: str | None = None
    role: str
    mfa_enabled: bool
    is_active: bool


class SessionResponse(BaseModel):
    """A newly issued session. The only place a token is ever returned."""

    user_id: UUID
    session_id: UUID
    token: str = Field(
        description=(
            "The session token. Send it as 'Authorization: Bearer <token>'. Returned "
            "once — ARGUS stores only its hash and cannot show it to you again."
        )
    )
    token_type: str = "Bearer"
    issued_at: datetime
    expires_at: datetime


class SessionSummary(BaseModel):
    """One of your own sessions. Deliberately has no token field."""

    session_id: UUID
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    active: bool


class EnrolmentResponse(BaseModel):
    """A started TOTP enrolment.

    `secret` is here because there is no other way to give it to the
    person setting up their authenticator, and it is the single response
    field in this service that carries credential material. MFA is not
    enabled until a code generated from it is verified — see `mfa.py` on
    why enrolment is two steps.
    """

    secret: str = Field(
        description="Base32 TOTP secret. Shown once, at enrolment. Store it in your app."
    )
    provisioning_uri: str = Field(
        description="otpauth:// URI for the same secret, for QR-code display."
    )
    mfa_enabled: bool = Field(
        default=False,
        description="Always false here. Enrolment completes when a code is verified.",
    )
