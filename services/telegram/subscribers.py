"""Who is subscribed. Four operations, no HTTP, no Telegram.

Separated from `app.py` for the reason every other ARGUS service
separates them: the routing layer should be readable as routing, and
these four functions should be testable without a client.

## Subscribe is an upsert, and `/stop` then `/start` is a resubscribe

`/start` from a chat already on the list is not an error and does not
duplicate — Telegram's own clients make it trivially easy to send twice,
and a bot that answered "you are already subscribed" as a failure would
be answering a question nobody asked. It clears `unsubscribed_at`, so a
returning subscriber becomes active again without losing the
`subscribed_at` that records when they first arrived.

## Deactivation has two callers with one meaning

`/stop` is one. The other is a 403 from Telegram during dispatch, which
means the user blocked the bot — the same fact, arrived at differently,
and it writes the same row. Keeping them one function is deliberate:
two ways to become inactive would eventually become two slightly
different definitions of active.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from infra.db.schema.telegram import telegram_subscribers

__all__ = ["Subscriber", "active_chat_ids", "deactivate", "get", "subscribe", "unsubscribe"]


@dataclass(frozen=True, slots=True)
class Subscriber:
    chat_id: int
    subscribed_at: datetime
    unsubscribed_at: datetime | None

    @property
    def active(self) -> bool:
        return self.unsubscribed_at is None


def subscribe(connection: Connection, chat_id: int, *, now: datetime | None = None) -> Subscriber:
    """Add a chat, or reactivate one that had stopped. Idempotent."""
    moment = now or datetime.now(UTC)
    statement = (
        insert(telegram_subscribers)
        .values(chat_id=chat_id, subscribed_at=moment, unsubscribed_at=None)
        .on_conflict_do_update(
            index_elements=[telegram_subscribers.c.chat_id],
            # `subscribed_at` is deliberately not touched: it records when
            # this chat first arrived, which stays true across a
            # stop-and-start.
            set_={"unsubscribed_at": None},
        )
        .returning(*telegram_subscribers.c)
    )
    return _row(connection.execute(statement).one())


def unsubscribe(
    connection: Connection, chat_id: int, *, now: datetime | None = None
) -> Subscriber | None:
    """Mark a chat inactive. Returns None if it was never subscribed.

    None rather than raising: `/stop` from a stranger is a coherent thing
    to receive and the correct answer is to do nothing, quietly.
    """
    return deactivate(connection, chat_id, now=now)


def deactivate(
    connection: Connection, chat_id: int, *, now: datetime | None = None
) -> Subscriber | None:
    """Set `unsubscribed_at` if it is not already set.

    Guarded on `unsubscribed_at IS NULL` so a second `/stop`, or a
    dispatch 403 for a chat that had already stopped, does not move the
    timestamp forward — the moment somebody left is a fact about them,
    not about the last time ARGUS noticed.
    """
    moment = now or datetime.now(UTC)
    row = connection.execute(
        telegram_subscribers.update()
        .where(
            telegram_subscribers.c.chat_id == chat_id,
            telegram_subscribers.c.unsubscribed_at.is_(None),
        )
        .values(unsubscribed_at=moment)
        .returning(*telegram_subscribers.c)
    ).one_or_none()
    if row is not None:
        return _row(row)
    return get(connection, chat_id)


def get(connection: Connection, chat_id: int) -> Subscriber | None:
    row = connection.execute(
        select(telegram_subscribers).where(telegram_subscribers.c.chat_id == chat_id)
    ).one_or_none()
    return _row(row) if row is not None else None


def active_chat_ids(connection: Connection) -> list[int]:
    """Every chat a dispatch run should message, oldest subscription first.

    Ordered so a run that is interrupted partway has messaged a
    deterministic prefix of the list rather than an arbitrary one, which
    makes "who got yesterday's alert" answerable without the sent log.
    """
    return list(
        connection.execute(
            select(telegram_subscribers.c.chat_id)
            .where(telegram_subscribers.c.unsubscribed_at.is_(None))
            .order_by(telegram_subscribers.c.subscribed_at, telegram_subscribers.c.chat_id)
        ).scalars()
    )


def _row(row) -> Subscriber:
    return Subscriber(
        chat_id=row.chat_id,
        subscribed_at=row.subscribed_at,
        unsubscribed_at=row.unsubscribed_at,
    )
