"""Identity (Module 22). The only service in ARGUS that issues credentials.

Every other service gets identity from here through exactly one function
— Module 19's `current_user_id`, whose body this module replaced and
whose signature and call sites it did not.
"""

from services.identity.accounts import (
    Account,
    account_for,
    change_password,
    change_role,
    deactivate,
    log_in,
    log_out,
    register,
)
from services.identity.app import create_app
from services.identity.attempts import (
    Lockout,
    RegistrationLockout,
    lockout_state,
    record_attempt,
    record_registration_attempt,
    registration_lockout_state,
)
from services.identity.audit import ACTIONS, CredentialInPayload, record
from services.identity.config import SECURITY, IdentityConfig, IdentitySettings
from services.identity.errors import IdentityError
from services.identity.mfa import Enrolment, begin_enrolment, complete_enrolment, verify_code
from services.identity.passwords import (
    PasswordTooWeak,
    hash_password,
    needs_rehash,
    verify_password,
)
from services.identity.roles import ADMIN, PUBLIC, REGISTERED_USER, require_role
from services.identity.seam import resolve_identity
from services.identity.sessions import IssuedSession, SessionRecord
from services.identity.tokens import BEARER_PREFIX, bearer_token, hash_token

__all__ = [
    "ACTIONS",
    "ADMIN",
    "BEARER_PREFIX",
    "PUBLIC",
    "REGISTERED_USER",
    "SECURITY",
    "Account",
    "CredentialInPayload",
    "Enrolment",
    "IdentityConfig",
    "IdentityError",
    "IdentitySettings",
    "IssuedSession",
    "Lockout",
    "PasswordTooWeak",
    "RegistrationLockout",
    "SessionRecord",
    "account_for",
    "begin_enrolment",
    "bearer_token",
    "change_password",
    "change_role",
    "complete_enrolment",
    "create_app",
    "deactivate",
    "hash_password",
    "hash_token",
    "lockout_state",
    "log_in",
    "log_out",
    "needs_rehash",
    "record",
    "record_attempt",
    "record_registration_attempt",
    "register",
    "registration_lockout_state",
    "require_role",
    "resolve_identity",
    "verify_code",
    "verify_password",
]
