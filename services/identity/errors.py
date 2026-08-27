"""The Identity service's error codes. The envelope lives in `services/shared/`.

## Every failure here says less than it knows

That is the design, not an oversight. `INVALID_CREDENTIALS` is returned
for an email that has no account and for an account whose password was
wrong, because a caller able to tell those apart has an account-existence
oracle — and an attacker with one stops guessing passwords against ten
thousand addresses and starts guessing them against the two hundred that
answered.

The same applies to `SESSION_INVALID`: expired, revoked, never existed
and malformed are one code. The client's next action is identical in all
four (log in again), and distinguishing them would let someone probe
which tokens once existed.

`detail` follows the same rule. Elsewhere in ARGUS it carries the
specifics a client needs; here it carries almost nothing, because the
specifics are the leak.

## The two that do say more, and why that is safe

`ACCOUNT_LOCKED` names when the lockout lifts. Withholding it would not
protect anything — the attacker already knows they are being blocked,
they are the one being blocked — and it turns a person locked out of
their own account from confused into informed.

`MFA_REQUIRED` and `MFA_CODE_REQUIRED` name what is missing, because the
caller has already proven the password. Telling somebody who has
authenticated that their account needs a second factor reveals nothing
they did not just demonstrate they were entitled to know.
"""

from __future__ import annotations

from services.shared.errors import ApiError, error_payload

__all__ = [
    "ACCOUNT_INACTIVE",
    "ACCOUNT_LOCKED",
    "EMAIL_TAKEN",
    "FORBIDDEN",
    "IDENTITY_REQUIRED",
    "INVALID_CREDENTIALS",
    "MFA_ALREADY_ENROLLED",
    "MFA_CODE_REQUIRED",
    "MFA_NOT_ENROLLED",
    "MFA_REQUIRED",
    "SESSION_INVALID",
    "WEAK_PASSWORD",
    "IdentityError",
    "error_payload",
]

#: Wrong password, or no such account. Deliberately indistinguishable.
INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
#: Too many recent failures. Names when the lockout lifts.
ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
#: The account exists and is disabled. Distinct from a bad password
#: because the caller proved the password before seeing this.
ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"
#: Registration against an address that already has an account.
EMAIL_TAKEN = "EMAIL_TAKEN"
#: The password does not meet the minimum ARGUS will store.
WEAK_PASSWORD = "WEAK_PASSWORD"
#: No session token was supplied for an endpoint that needs one.
IDENTITY_REQUIRED = "IDENTITY_REQUIRED"
#: Expired, revoked, forged or malformed. One code for all four.
SESSION_INVALID = "SESSION_INVALID"
#: Authenticated, but this role may not do this.
FORBIDDEN = "FORBIDDEN"
#: The password was right and this account needs a TOTP code too.
MFA_CODE_REQUIRED = "MFA_CODE_REQUIRED"
#: The role requires an enrolled second factor and this account has none.
MFA_REQUIRED = "MFA_REQUIRED"
MFA_NOT_ENROLLED = "MFA_NOT_ENROLLED"
MFA_ALREADY_ENROLLED = "MFA_ALREADY_ENROLLED"


class IdentityError(ApiError):
    """An Identity error.

    Subclassed rather than aliased for the reason Modules 19 and 20
    subclass: `app.py`'s handler must catch this service's errors and not
    another service's.
    """


def invalid_credentials() -> IdentityError:
    """The one refusal that must never say which half was wrong."""
    return IdentityError(
        INVALID_CREDENTIALS,
        "Email or password is incorrect.",
        status=401,
    )
