"""The core PIT-safe selection primitive.

Every entity-specific query in this package (`ohlcv.py`, `fundamentals.py`,
`corporate_actions.py`, `feature_vectors.py`) is built on
`select_latest_as_of`. It exists so the enforcement rule is written and
tested in exactly one place: **filter on `availability_time`, never
`event_time` or `observation_time`; among rows that pass the filter, take
the one with the greatest `availability_time`.**

That second half is the restatement rule. A logical record — one
security, one fiscal period, one statement type — can have several rows
(Module 05: a restatement is a new row, never an edit). At any given
`as_of`, some of those rows are knowable and some are not; among the
knowable ones, the *latest* is what a query made on that date would
actually have seen. Taking the first match, or the row with the greatest
`event_time`, or the unconditionally latest row regardless of `as_of`,
are all different bugs this function is the single place that must not
have.

Deliberately generic over the table rather than one function per entity:
a second entity added later gets the correct behaviour for free by
calling this, instead of a fifth hand-written copy of the same six-line
query with a subtly different mistake in it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Table, and_, select
from sqlalchemy.engine import Connection, Row


def select_latest_as_of(
    connection: Connection,
    table: Table,
    *,
    key: dict[str, Any],
    as_of: datetime,
    availability_column: str = "availability_time",
    precedence: Sequence[str] = (),
) -> Row | None:
    """The row for `key` with the greatest `availability_column` <= `as_of`.

    `key` identifies the logical record (e.g. `{"security_id": ...,
    "statement_type": ..., "fiscal_period": ...}`) — everything a
    restatement of the *same* fact shares. Returns None if no row for
    that key is knowable by `as_of`; the caller wraps that in an
    `AsOfResult` miss rather than treating None as "the value is null".

    `precedence` names columns ordered **descending before**
    `availability_column`, for the case where `key` deliberately does not
    pin a single logical record. `get_latest_fundamental_as_of` is the
    motivating caller: it asks for "the most recently *ended* period that
    was knowable", so it keys on `(security_id, statement_type)` and
    passes `precedence=("fiscal_period_end",)`. Without it, a restatement
    of an *older* period filed after a newer period's original filing
    would win on `availability_time` alone and the query would silently
    return the wrong quarter.

    It changes only the ordering, never the filter — every caller gets the
    same `availability_column <= as_of` enforcement regardless. Defaulting
    to empty leaves the emitted SQL byte-identical for callers that do not
    pass it.
    """
    column = table.c[availability_column]
    conditions = [table.c[name] == value for name, value in key.items()]
    conditions.append(column <= as_of)

    ordering = [table.c[name].desc() for name in precedence]
    ordering.append(column.desc())

    query = select(table).where(and_(*conditions)).order_by(*ordering).limit(1)
    return connection.execute(query).first()


def select_all_as_of(
    connection: Connection,
    table: Table,
    *,
    key: dict[str, Any],
    as_of: datetime,
    availability_column: str = "availability_time",
    order_by: str | None = None,
) -> list[Row]:
    """Every row for `key` knowable by `as_of` — for one-to-many entities
    (e.g. every corporate action for a security), not a single logical
    record with restatements.

    Still filters on `availability_column <= as_of` and nothing else —
    the same enforcement, just without collapsing to one row.
    """
    column = table.c[availability_column]
    conditions = [table.c[name] == value for name, value in key.items()]
    conditions.append(column <= as_of)

    query = select(table).where(and_(*conditions))
    query = query.order_by(table.c[order_by]) if order_by else query.order_by(column)
    return list(connection.execute(query))
