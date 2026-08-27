"""RBAC: three roles, one rank order, one check.

## Module 03's single-FK design is still right, and here is the test of it

Module 03 gave `users` a single `role_id` rather than a join table and
judged it adequate for the three-role MVP. Building the authorization
layer on it is the first real test of that call, and it holds — with one
observation worth recording rather than acting on.

It holds because the three roles are **totally ordered**. `public` <
`registered_user` < `admin`, each strictly containing the last, so every
question this module needs to answer is "is this user's rank at least X",
which one column answers exactly. A join table would model a set, and a
set is only worth its complexity when permissions are not nested.

The observation: the moment two roles exist that are *not* nested — an
`analyst` who can review scans but not manage users, alongside a
`billing_admin` who can do the reverse — the rank collapses and the
single FK stops being expressive. That is a real fork in the road, and
the honest thing is that ARGUS is nowhere near it. When it arrives it is
a migration and a rewrite of `has_at_least`, both of which are small
because everything goes through this file.

## `public` is a real row that is never assigned

It is seeded by migration 0012 and no user should hold it: an
unauthenticated caller has no `users` row at all, so `public` names the
privileges of a *request*, not of a person. It exists as a row so the set
of roles is enumerable from the database rather than only from Python,
and so `rank_of("public") == 0` is a fact rather than a special case
sprinkled through the checks.

## The check refuses by default

`require_role` raises unless the rank is sufficient. There is no
"permissive mode", no bypass flag, and no configuration that turns it
off — a switch that disables authorization is a switch that will one day
be left in the wrong position, and this module's entire purpose is being
the thing that cannot be left in the wrong position.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from infra.db.schema.users import roles, users
from services.identity.errors import FORBIDDEN, MFA_REQUIRED, IdentityError

__all__ = [
    "ADMIN",
    "PUBLIC",
    "RANKS",
    "REGISTERED_USER",
    "ROLE_NAMES",
    "has_at_least",
    "rank_of",
    "require_role",
    "role_id_for",
    "role_of",
]

PUBLIC = "public"
REGISTERED_USER = "registered_user"
ADMIN = "admin"

#: Total order. The reason one `role_id` column is sufficient — see the
#: module docstring on what would break it.
RANKS: dict[str, int] = {PUBLIC: 0, REGISTERED_USER: 1, ADMIN: 2}

ROLE_NAMES: tuple[str, ...] = (PUBLIC, REGISTERED_USER, ADMIN)


class UnknownRole(ValueError):
    """A role name outside the three ARGUS recognises."""


def rank_of(name: str) -> int:
    if name not in RANKS:
        raise UnknownRole(
            f"{name!r} is not an ARGUS role. The three are {', '.join(ROLE_NAMES)} — "
            "Copy and Institutional roles are deferred until those products exist."
        )
    return RANKS[name]


def has_at_least(actual: str, required: str) -> bool:
    """Whether `actual` confers at least `required`'s privileges."""
    return rank_of(actual) >= rank_of(required)


def role_id_for(connection: Connection, name: str) -> UUID:
    """The `roles` row id for a name. Seeded by migration 0012."""
    rank_of(name)  # refuse an unknown name before touching the database
    role_id = connection.execute(
        select(roles.c.id).where(roles.c.name == name)
    ).scalar_one_or_none()
    if role_id is None:
        raise UnknownRole(
            f"Role {name!r} is not in the database. Migration 0012 seeds the three "
            "ARGUS roles; a deployment missing them cannot register anybody."
        )
    return role_id


def role_of(connection: Connection, user_id: UUID) -> str | None:
    """This user's role name, or None if the user does not exist."""
    return connection.execute(
        select(roles.c.name)
        .select_from(users.join(roles, users.c.role_id == roles.c.id))
        .where(users.c.id == user_id)
    ).scalar_one_or_none()


def require_role(connection: Connection, user_id: UUID, required: str) -> str:
    """Refuse unless this user holds at least `required`. Returns their role.

    Two refusals, and the second is the one that makes the MFA decision
    real rather than advisory: an `admin` who has not enrolled a second
    factor is refused admin-gated actions with `MFA_REQUIRED`. They can
    still log in and still enrol — locking them out of enrolment would
    make the requirement unsatisfiable — but the elevated surface stays
    closed until they do.
    """
    actual = role_of(connection, user_id)
    if actual is None:
        raise IdentityError(
            FORBIDDEN,
            "This action requires a role and the acting user has none.",
            status=403,
        )

    if not has_at_least(actual, required):
        raise IdentityError(
            FORBIDDEN,
            f"This action requires the {required} role.",
            status=403,
            detail={"required": required},
        )

    if required == ADMIN and not _mfa_enabled(connection, user_id):
        raise IdentityError(
            MFA_REQUIRED,
            "Admin actions require an enrolled second factor. Enrol TOTP at "
            "/identity/mfa/enroll and verify it, then retry.",
            status=403,
            detail={"required": required},
        )

    return actual


def _mfa_enabled(connection: Connection, user_id: UUID) -> bool:
    return bool(
        connection.execute(
            select(users.c.mfa_enabled).where(users.c.id == user_id)
        ).scalar_one_or_none()
    )
