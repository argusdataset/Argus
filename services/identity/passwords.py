"""Hashing passwords, and the two things this file exists to make impossible.

## One: a password never becomes a string anything else can see

`hash_password` takes a password and returns a hash. Nothing here returns
a password, logs one, puts one in an exception, or stores one anywhere.
`PasswordTooWeak` carries the length requirement and the length supplied,
never the value — an error message is the most commonly logged string in
any service, and a password inside one ends up in a log aggregator,
a bug report and a screenshot.

The argon2 encoded hash carries its own salt and parameters, so there is
no second column to keep in sync and no way to verify against the wrong
cost by accident.

## Two: a wrong email and a wrong password take the same time

`verify_password` accepts `stored=None` — the shape you get when the
email named no account — and still performs a real argon2 verification
against a fixed dummy hash before returning False. Without it, a
non-existent account answers in microseconds and a real one in ~50ms,
and that difference is a remote account-existence oracle that no amount
of care in `errors.py` can close. `errors.py` makes the two answers say
the same thing; this makes them take the same time.

The dummy hash is computed once at import against the same parameters, so
it stays honest if the parameters change.

## Why argon2id and not bcrypt

Both are acceptable and the brief allows either. Argon2id because it is
memory-hard: bcrypt's cost is CPU time only, and an attacker with GPUs
gets a much better return on bcrypt than on a hash that demands 19 MiB
per guess. It also won the Password Hashing Competition and is what OWASP
names first. Bcrypt's one real advantage — being older — is worth less
than memory-hardness against the attacker who matters here, the one who
has already stolen the table.

Bcrypt also silently truncates at 72 bytes, which is the kind of quiet
behaviour this project has spent twenty-one modules avoiding.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exceptions
from argon2.low_level import Type

from services.identity.config import IdentitySettings

__all__ = [
    "PasswordTooWeak",
    "hash_password",
    "hasher_for",
    "needs_rehash",
    "verify_password",
]


class PasswordTooWeak(ValueError):
    """The password is shorter than ARGUS will store.

    Carries the requirement and the length supplied. Never the password —
    this message reaches logs.
    """

    def __init__(self, supplied_length: int, minimum: int) -> None:
        super().__init__(
            f"Password must be at least {minimum} characters; got {supplied_length}. "
            "ARGUS enforces length and nothing else: composition rules push people "
            "towards predictable substitutions without measurably helping."
        )
        self.supplied_length = supplied_length
        self.minimum = minimum


def hasher_for(settings: IdentitySettings | None = None) -> PasswordHasher:
    """An argon2id hasher built from the configured cost parameters.

    Built from config rather than the library defaults so that changing a
    cost is a change to one enumerable `security` setting, and so the
    parameters a deployment is actually running are readable from
    `IdentitySettings.security()`.
    """
    settings = settings or IdentitySettings()
    return PasswordHasher(
        time_cost=int(settings.argon2_time_cost),
        memory_cost=int(settings.argon2_memory_kib),
        parallelism=int(settings.argon2_parallelism),
        type=Type.ID,
    )


def hash_password(password: str, *, settings: IdentitySettings | None = None) -> str:
    """The encoded argon2id hash. Salt and parameters travel inside it.

    Raises `PasswordTooWeak` before hashing rather than after, so a
    rejected password is never put through the KDF and never lands in a
    timing profile alongside accepted ones.
    """
    settings = settings or IdentitySettings()
    minimum = int(settings.min_password_length)
    if len(password) < minimum:
        raise PasswordTooWeak(len(password), minimum)
    return hasher_for(settings).hash(password)


def verify_password(
    password: str, stored: str | None, *, settings: IdentitySettings | None = None
) -> bool:
    """Whether the password matches. False for a missing hash, in equal time.

    `stored=None` is the shape returned when the email named no account,
    or when an account exists with no credential set. Both still pay for a
    real verification against `_DUMMY_HASH` — see the module docstring on
    why answering "no such user" quickly is a remotely observable leak.
    """
    settings = settings or IdentitySettings()
    hasher = hasher_for(settings)
    target = stored if stored is not None else _dummy_hash(settings)
    try:
        hasher.verify(target, password)
    except (
        argon2_exceptions.VerifyMismatchError,
        argon2_exceptions.VerificationError,
        argon2_exceptions.InvalidHashError,
    ):
        return False
    # A match against the dummy is not a match against anything real. It
    # cannot happen — nothing knows the dummy password — and returning
    # False for it anyway means a future refactor that leaks the constant
    # does not become an authentication bypass.
    return stored is not None


def needs_rehash(stored: str, *, settings: IdentitySettings | None = None) -> bool:
    """Whether this hash was made under weaker parameters than are configured now.

    Raising a cost parameter leaves every existing hash at the old one.
    Nothing calls this yet; `accounts.py` documents where a login-time
    upgrade would go, and doing it silently on login is the only moment
    the plaintext is available to rehash with.
    """
    return hasher_for(settings).check_needs_rehash(stored)


#: A real hash of a value nothing knows, used to make a failed lookup
#: cost the same as a failed password. Computed lazily and cached per
#: parameter set so a settings change cannot leave it hashed under the
#: old cost — which would reintroduce the timing difference it exists to
#: remove.
_DUMMY_CACHE: dict[tuple[int, int, int], str] = {}


def _dummy_hash(settings: IdentitySettings) -> str:
    key = (
        int(settings.argon2_time_cost),
        int(settings.argon2_memory_kib),
        int(settings.argon2_parallelism),
    )
    if key not in _DUMMY_CACHE:
        _DUMMY_CACHE[key] = hasher_for(settings).hash("argus-timing-equaliser-not-a-credential")
    return _DUMMY_CACHE[key]
