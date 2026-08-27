"""Session tokens: how they are made, and why they are hashed differently.

## The asymmetry with `passwords.py` is deliberate

Passwords get argon2id with 19 MiB of memory per verification. Session
tokens get one pass of SHA-256. That looks inconsistent and is not.

A slow hash exists to make *guessing* expensive. Guessing works on
passwords because people choose them from a space an attacker can
enumerate. A session token is 256 bits from `os.urandom`; there is no
dictionary, no distribution to exploit, and no number of GPUs that makes
enumerating 2^256 anything other than impossible. Argon2 would add ~25ms
to every authenticated request in exchange for hardening against an
attack that cannot be run.

What the hash *is* for is the same in both cases: a stolen `sessions`
table must not be a stolen set of live credentials. SHA-256 delivers that
completely for a random 256-bit input — there is nothing to reverse
without the preimage.

Module 03 anticipated exactly this: `sessions.token_hash` is unique and
the column comment says "Hash, never the token itself".

## The token is returned once and never again

`issue` returns `(token, token_hash)`. The caller hands the token to the
person who logged in and stores the hash. Nothing in ARGUS can recover a
token from the database afterwards, which means nothing in ARGUS can leak
one — not a support tool, not an admin endpoint, not a debug log. A user
who loses their token logs in again.

## Comparison

Lookup is by hash equality on a unique index, so the database does the
comparison and there is no Python-level secret comparison to get wrong.
`same_token` exists for the one place a caller holds two hashes and wants
to compare them without thinking about it, and uses `compare_digest`
because writing `==` on a credential-derived value is a habit worth not
having.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from services.identity.config import IdentitySettings

__all__ = ["BEARER_PREFIX", "bearer_token", "hash_token", "issue", "same_token"]

#: The scheme in the `Authorization` header. Standard, and specifically
#: not a custom header — proxies, log scrubbers and client libraries all
#: know to treat `Authorization` as a secret, and know nothing about
#: `X-Argus-Anything`.
BEARER_PREFIX = "Bearer "


def issue(*, settings: IdentitySettings | None = None) -> tuple[str, str]:
    """A new session token and its hash. The token is never derivable again.

    `token_urlsafe` draws from `os.urandom`, which is the platform CSPRNG
    — not `random`, which is a Mersenne Twister whose internal state can
    be recovered from its output and would make every future session
    token predictable from a handful of past ones.
    """
    settings = settings or IdentitySettings()
    token = secrets.token_urlsafe(int(settings.session_token_bytes))
    return token, hash_token(token)


def hash_token(token: str) -> str:
    """What is stored. See the module docstring on why this is not argon2."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(authorization: str | None) -> str | None:
    """The token out of an `Authorization` header, or None.

    None for absent, for a non-Bearer scheme, and for `Bearer` with
    nothing after it. A caller cannot distinguish those and does not need
    to: all three mean no credential was presented.
    """
    if not authorization:
        return None
    if not authorization.startswith(BEARER_PREFIX):
        return None
    token = authorization[len(BEARER_PREFIX) :].strip()
    return token or None


def same_token(left: str, right: str) -> bool:
    """Constant-time comparison of two token hashes."""
    return hmac.compare_digest(left, right)
