"""User watchlists: user-created, named, manually edited. CRUD and nothing more.

## The boundary that this file exists on the correct side of

ARGUS has two things called watchlists and they are not the same thing:

- **User Watchlists** — these. A person makes one, names it "Long Term",
  puts things on it, takes things off. Stored in `user_watchlists`.
- **ARGUS Intelligence Watchlists** — the three auto-generated lists
  (DOWN TREND / CONSOLIDATION / BREAKOUT READY) derived from
  `market_state`. Module 21's, not built, and **not built here, not even
  read-only.**

That distinction has been stated as non-negotiable since early planning,
specifically to stop this module drifting into that one. Nothing in this
file imports from `core/market_state/` and a structural test asserts it.

## A watchlist holds identities, not tickers

Module 03 made `user_watchlist_items.security_id` a foreign key to
`security_identity` rather than storing a ticker string, and the comment
there says why: a watchlist entry survives a ticker change like
everything else in ARGUS. A user who added a company under one symbol
still has that company on their list after a rename.

The API takes tickers, because that is what a person types. It resolves
them once, on the way in, and stores identity.

## Not found and not yours are the same answer

Asking for a watchlist that belongs to somebody else returns
`WATCHLIST_NOT_FOUND`, exactly as asking for one that does not exist
does. A 403 would confirm the list exists, which leaks the fact that some
other user has a list with that id — and every query here filters on
`user_id` in the same statement rather than fetching and then checking,
so there is no code path where the ownership test can be forgotten.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from data.normalization.identity import SecurityIdentityResolver
from infra.db.schema.identity import security_identity, security_ticker_history
from infra.db.schema.users import user_watchlist_items, user_watchlists
from services.terminal.config import TerminalConfig
from services.terminal.errors import (
    WATCHLIST_LIMIT_REACHED,
    WATCHLIST_NAME_TAKEN,
    WATCHLIST_NOT_FOUND,
    TerminalError,
    security_not_found,
)
from services.terminal.schemas import WatchlistDetail, WatchlistItem, WatchlistSummary

__all__ = [
    "add_security",
    "create_watchlist",
    "delete_watchlist",
    "list_watchlists",
    "read_watchlist",
    "remove_security",
    "rename_watchlist",
]


def list_watchlists(connection: Connection, user_id: UUID) -> list[WatchlistSummary]:
    """This user's lists, newest first, with counts but not contents.

    One query with a correlated count rather than one query per list: a
    sidebar showing fifty lists should not cost fifty round trips.
    """
    item_count = (
        select(func.count())
        .select_from(user_watchlist_items)
        .where(user_watchlist_items.c.watchlist_id == user_watchlists.c.id)
        .scalar_subquery()
    )
    rows = connection.execute(
        select(
            user_watchlists.c.id,
            user_watchlists.c.name,
            user_watchlists.c.created_at,
            user_watchlists.c.updated_at,
            item_count.label("item_count"),
        )
        .where(user_watchlists.c.user_id == user_id)
        .order_by(desc(user_watchlists.c.created_at))
    ).all()

    return [
        WatchlistSummary(
            id=row.id,
            name=row.name,
            item_count=int(row.item_count),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


def create_watchlist(
    connection: Connection,
    user_id: UUID,
    name: str,
    *,
    config: TerminalConfig | None = None,
) -> WatchlistDetail:
    """Create an empty named list."""
    config = config or TerminalConfig()
    cleaned = _clean_name(name, config)

    existing = connection.execute(
        select(func.count())
        .select_from(user_watchlists)
        .where(user_watchlists.c.user_id == user_id)
    ).scalar_one()
    if existing >= int(config.limits.max_watchlists_per_user):
        raise TerminalError(
            WATCHLIST_LIMIT_REACHED,
            f"You already have {existing} watchlists; the limit is "
            f"{int(config.limits.max_watchlists_per_user)}.",
            status=409,
            detail={"limit": int(config.limits.max_watchlists_per_user)},
        )

    try:
        row = connection.execute(
            user_watchlists.insert()
            .values(user_id=user_id, name=cleaned)
            .returning(
                user_watchlists.c.id,
                user_watchlists.c.name,
                user_watchlists.c.created_at,
                user_watchlists.c.updated_at,
            )
        ).one()
    except IntegrityError as error:
        # `uq_user_watchlist_name` is per user, so this is only ever this
        # user's own collision.
        raise _name_taken(cleaned) from error

    return WatchlistDetail(
        id=row.id,
        name=row.name,
        created_at=row.created_at,
        updated_at=row.updated_at,
        items=[],
    )


def rename_watchlist(
    connection: Connection,
    user_id: UUID,
    watchlist_id: UUID,
    name: str,
    *,
    config: TerminalConfig | None = None,
) -> WatchlistDetail:
    """Rename a list. Ownership is in the WHERE clause, not a later check."""
    config = config or TerminalConfig()
    cleaned = _clean_name(name, config)

    try:
        row = connection.execute(
            update(user_watchlists)
            .where(
                user_watchlists.c.id == watchlist_id,
                user_watchlists.c.user_id == user_id,
            )
            .values(name=cleaned, updated_at=datetime.now(UTC))
            .returning(user_watchlists.c.id)
        ).one_or_none()
    except IntegrityError as error:
        raise _name_taken(cleaned) from error

    if row is None:
        raise _not_found(watchlist_id)
    return read_watchlist(connection, user_id, watchlist_id)


def delete_watchlist(connection: Connection, user_id: UUID, watchlist_id: UUID) -> None:
    """Delete a list and its contents.

    A real delete, not a soft one. ARGUS's never-delete rule is about
    *market history* — a failed setup, a signal, an outcome — because
    erasing those would rewrite the record the system is judged on. A
    person's watchlist is their own working note, and refusing to let them
    throw it away would be applying a research guarantee to a UI
    preference. `user_watchlist_items` cascades.
    """
    deleted = connection.execute(
        delete(user_watchlists).where(
            user_watchlists.c.id == watchlist_id,
            user_watchlists.c.user_id == user_id,
        )
    ).rowcount
    if not deleted:
        raise _not_found(watchlist_id)


def read_watchlist(connection: Connection, user_id: UUID, watchlist_id: UUID) -> WatchlistDetail:
    """One list with its contents, in the user's order.

    Each item carries the ticker *currently* valid for its security, which
    is a display concern — the stored fact is the identity, and this is
    how it looks today.
    """
    header = connection.execute(
        select(
            user_watchlists.c.id,
            user_watchlists.c.name,
            user_watchlists.c.created_at,
            user_watchlists.c.updated_at,
        ).where(
            user_watchlists.c.id == watchlist_id,
            user_watchlists.c.user_id == user_id,
        )
    ).one_or_none()
    if header is None:
        raise _not_found(watchlist_id)

    now = datetime.now(UTC)
    rows = connection.execute(
        select(
            user_watchlist_items.c.security_id,
            user_watchlist_items.c.position,
            user_watchlist_items.c.added_at,
            security_ticker_history.c.ticker,
            security_identity.c.name,
        )
        .select_from(
            user_watchlist_items.join(
                security_identity,
                security_identity.c.id == user_watchlist_items.c.security_id,
            ).outerjoin(
                security_ticker_history,
                (security_ticker_history.c.security_id == user_watchlist_items.c.security_id)
                & (security_ticker_history.c.valid_from <= now)
                & (
                    security_ticker_history.c.valid_to.is_(None)
                    | (security_ticker_history.c.valid_to > now)
                ),
            )
        )
        .where(user_watchlist_items.c.watchlist_id == watchlist_id)
        .order_by(
            user_watchlist_items.c.position.nullslast(),
            user_watchlist_items.c.added_at,
        )
    ).all()

    return WatchlistDetail(
        id=header.id,
        name=header.name,
        created_at=header.created_at,
        updated_at=header.updated_at,
        items=[
            WatchlistItem(
                security_id=row.security_id,
                ticker=row.ticker,
                name=row.name,
                position=row.position,
                added_at=row.added_at,
            )
            for row in rows
        ],
    )


def add_security(
    connection: Connection,
    user_id: UUID,
    watchlist_id: UUID,
    ticker: str,
    *,
    position: int | None = None,
    config: TerminalConfig | None = None,
) -> WatchlistDetail:
    """Add a security by ticker. Idempotent — adding twice is not an error.

    Idempotent on purpose: a user double-tapping "add" has expressed one
    intention, and the second tap should leave them where they wanted to
    be rather than showing them a conflict they cannot act on.
    """
    config = config or TerminalConfig()
    _require_owned(connection, user_id, watchlist_id)

    resolver = SecurityIdentityResolver(connection)
    security_id = resolver.try_resolve(ticker, datetime.now(UTC))
    if security_id is None:
        raise security_not_found(ticker)

    count = connection.execute(
        select(func.count())
        .select_from(user_watchlist_items)
        .where(user_watchlist_items.c.watchlist_id == watchlist_id)
    ).scalar_one()

    already = connection.execute(
        select(user_watchlist_items.c.id).where(
            user_watchlist_items.c.watchlist_id == watchlist_id,
            user_watchlist_items.c.security_id == security_id,
        )
    ).scalar_one_or_none()

    if already is None:
        if count >= int(config.limits.max_watchlist_items):
            raise TerminalError(
                WATCHLIST_LIMIT_REACHED,
                f"This watchlist holds {count} securities; the limit is "
                f"{int(config.limits.max_watchlist_items)}.",
                status=409,
                detail={"limit": int(config.limits.max_watchlist_items)},
            )
        connection.execute(
            user_watchlist_items.insert().values(
                watchlist_id=watchlist_id,
                security_id=security_id,
                position=position if position is not None else count,
            )
        )
        _touch(connection, watchlist_id)

    return read_watchlist(connection, user_id, watchlist_id)


def remove_security(
    connection: Connection, user_id: UUID, watchlist_id: UUID, ticker: str
) -> WatchlistDetail:
    """Remove a security by ticker. Also idempotent.

    Resolves the ticker without an `as_of` bound deliberately: a user
    removing a name that has since been renamed should still be able to
    take it off their list.
    """
    _require_owned(connection, user_id, watchlist_id)

    resolver = SecurityIdentityResolver(connection)
    security_id = resolver.try_resolve(ticker, datetime.now(UTC))
    if security_id is None:
        raise security_not_found(ticker)

    removed = connection.execute(
        delete(user_watchlist_items).where(
            user_watchlist_items.c.watchlist_id == watchlist_id,
            user_watchlist_items.c.security_id == security_id,
        )
    ).rowcount
    if removed:
        _touch(connection, watchlist_id)

    return read_watchlist(connection, user_id, watchlist_id)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _require_owned(connection: Connection, user_id: UUID, watchlist_id: UUID) -> None:
    owned = connection.execute(
        select(user_watchlists.c.id).where(
            user_watchlists.c.id == watchlist_id,
            user_watchlists.c.user_id == user_id,
        )
    ).scalar_one_or_none()
    if owned is None:
        raise _not_found(watchlist_id)


def _touch(connection: Connection, watchlist_id: UUID) -> None:
    connection.execute(
        update(user_watchlists)
        .where(user_watchlists.c.id == watchlist_id)
        .values(updated_at=datetime.now(UTC))
    )


def _clean_name(name: str, config: TerminalConfig) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise TerminalError(
            "INVALID_REQUEST", "A watchlist needs a name.", status=422, detail={"field": "name"}
        )
    ceiling = int(config.limits.max_watchlist_name_length)
    if len(cleaned) > ceiling:
        raise TerminalError(
            "INVALID_REQUEST",
            f"A watchlist name may be at most {ceiling} characters.",
            status=422,
            detail={"field": "name", "max_length": ceiling},
        )
    return cleaned


def _not_found(watchlist_id: UUID) -> TerminalError:
    return TerminalError(
        WATCHLIST_NOT_FOUND,
        f"No watchlist {watchlist_id}.",
        status=404,
        detail={"watchlist_id": str(watchlist_id)},
    )


def _name_taken(name: str) -> TerminalError:
    return TerminalError(
        WATCHLIST_NAME_TAKEN,
        f"You already have a watchlist called {name!r}.",
        status=409,
        detail={"name": name},
    )
