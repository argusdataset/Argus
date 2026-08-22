"""Duplicate detection in canonical data.

Not the same failure mode a unique constraint already prevents. Module 03
guarantees uniqueness on `(security_id, timeframe, event_time,
observation_time)` for OHLCV — two rows can never be byte-identical at
the database level. What this module catches instead: two rows for the
*same* logical record (same event, different `observation_time`) whose
values are identical, meaning the second row is not a genuine
restatement — new information arriving later — but a spurious
re-ingestion (e.g. a cache miss that re-fetched and re-persisted a value
that had not actually changed). That is not a correctness bug on its own
— PIT queries still return a defensible value — but it inflates the
observation history with rows that carry no new information, and it is
worth surfacing for the same reason gaps are: an operator should be able
to see it.

Advisory, like `gaps.py`: reported, never deleted or merged. Deciding
whether a duplicate should be pruned is an operational choice outside
this module's scope, and the append-only guard on `canonical_ohlcv` would
refuse a delete anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from data.canonical_model.records import CanonicalTimeframe
from infra.db.schema.canonical import canonical_ohlcv


@dataclass(frozen=True, slots=True)
class DuplicateBar:
    """Two (or more) rows for the same bar whose values do not differ."""

    security_id: UUID
    bar_date: date
    observation_times: tuple[datetime, ...]


def detect_duplicate_bars(
    connection: Connection,
    security_id: UUID,
    *,
    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY,
) -> list[DuplicateBar]:
    """Bars for `security_id` with more than one row and no value change.

    A restatement with an actually-different raw close is not a
    duplicate — a real correction produced it, and it is exactly what the
    PIT layer's restatement handling exists to serve correctly. Only rows
    that carry the same raw OHLCV values are flagged here.
    """
    rows = connection.execute(
        select(
            canonical_ohlcv.c.event_time,
            canonical_ohlcv.c.observation_time,
            canonical_ohlcv.c.open_raw,
            canonical_ohlcv.c.high_raw,
            canonical_ohlcv.c.low_raw,
            canonical_ohlcv.c.close_raw,
            canonical_ohlcv.c.volume_raw,
        )
        .where(
            canonical_ohlcv.c.security_id == security_id,
            canonical_ohlcv.c.timeframe == timeframe.value,
        )
        .order_by(canonical_ohlcv.c.event_time, canonical_ohlcv.c.observation_time)
    ).all()

    by_date: dict[date, list] = {}
    for row in rows:
        by_date.setdefault(row.event_time.date(), []).append(row)

    duplicates: list[DuplicateBar] = []
    for bar_date, bar_rows in by_date.items():
        if len(bar_rows) < 2:
            continue
        first = bar_rows[0]
        identical = [
            row
            for row in bar_rows
            if (row.open_raw, row.high_raw, row.low_raw, row.close_raw, row.volume_raw)
            == (first.open_raw, first.high_raw, first.low_raw, first.close_raw, first.volume_raw)
        ]
        if len(identical) > 1:
            duplicates.append(
                DuplicateBar(
                    security_id=security_id,
                    bar_date=bar_date,
                    observation_times=tuple(row.observation_time for row in identical),
                )
            )

    return duplicates
