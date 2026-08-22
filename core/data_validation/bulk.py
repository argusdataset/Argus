"""Bulk point-in-time loads for whole-universe reads.

**Added in Module 08, extending Module 07.** It lives here rather than in
`core/feature_engine/` deliberately: Module 07's central promise is that
the enforcement rule — *filter on `availability_time`, take the latest
surviving row per logical record* — is written in exactly one module. A
vectorized feature engine cannot call the single-security `get_as_of` ten
thousand times per date, so it needs bulk variants; putting those
anywhere else would mean a second copy of the enforcement rule, which is
precisely what Module 07 exists to prevent.

Same rule, same guarantees, different arity:

- `get_as_of` (query.py) → one security, one logical record, typed result.
- these → many securities, many rows, a DataFrame, one SQL round trip.

The "latest surviving row per logical record" half is done with
PostgreSQL's `DISTINCT ON`, which is what makes it a single query over the
whole universe rather than N queries or a client-side group-by over a
result set that could be tens of millions of rows.

Missing data is reported, never zero-filled — a security absent from the
returned frame is absent because nothing was knowable for it as of the
requested date, and the caller must be able to tell that apart from a
genuine zero.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from data.canonical_model.records import CanonicalStatementType, CanonicalTimeframe

#: Columns returned by `load_ohlcv_panel_as_of`.
OHLCV_COLUMNS = (
    "security_id",
    "event_time",
    "availability_time",
    "open_raw",
    "high_raw",
    "low_raw",
    "close_raw",
    "volume_raw",
)


def load_ohlcv_panel_as_of(
    connection: Connection,
    security_ids: list[UUID],
    as_of: datetime,
    *,
    start: datetime | None = None,
    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY,
) -> pd.DataFrame:
    """Every bar for `security_ids` knowable at `as_of`, as one long frame.

    `DISTINCT ON (security_id, event_time)` with `ORDER BY ...,
    availability_time DESC` applies the restatement rule inside the
    database: for each bar, the most recent revision that was actually
    knowable by `as_of` wins, and revisions filed later are invisible.

    `start` bounds the history loaded. A feature engine only ever needs
    its longest lookback window, and loading fifteen years of the full
    universe to compute one date's features would be gratuitous.

    Returns a long frame (one row per security per bar). Empty when
    nothing is knowable — an empty frame, never a fabricated one.
    """
    if not security_ids:
        return pd.DataFrame(columns=list(OHLCV_COLUMNS))

    sql = """
        SELECT DISTINCT ON (security_id, event_time)
               security_id, event_time, availability_time,
               open_raw, high_raw, low_raw, close_raw, volume_raw
        FROM canonical_ohlcv
        WHERE security_id = ANY(:security_ids)
          AND timeframe = :timeframe
          AND availability_time <= :as_of
          {start_clause}
        ORDER BY security_id, event_time, availability_time DESC
    """.format(start_clause="AND event_time >= :start" if start else "")

    params: dict[str, object] = {
        "security_ids": [str(value) for value in security_ids],
        "timeframe": timeframe.value,
        "as_of": as_of,
    }
    if start:
        params["start"] = start

    rows = connection.execute(text(sql), params).all()
    frame = pd.DataFrame(rows, columns=list(OHLCV_COLUMNS))
    if frame.empty:
        return frame

    for column in ("open_raw", "high_raw", "low_raw", "close_raw", "volume_raw"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def load_corporate_actions_as_of(
    connection: Connection,
    security_ids: list[UUID],
    as_of: datetime,
) -> pd.DataFrame:
    """Every corporate action for `security_ids` knowable at `as_of`.

    The bulk equivalent of `get_corporate_actions_as_of`. This is the
    load-bearing one for Module 08: adjustment factors must be built from
    *only* these rows. Module 05's `apply_adjustments` applies every
    action ever ingested, which for a historical `as_of` would let a 2015
    price series reflect a split announced in 2020.
    """
    empty_columns = [
        "security_id",
        "action_type",
        "effective_date",
        "availability_time",
        "details",
    ]
    if not security_ids:
        return pd.DataFrame(columns=empty_columns)

    sql = """
        SELECT security_id, action_type, effective_date, availability_time, details
        FROM canonical_corporate_actions
        WHERE security_id = ANY(:security_ids)
          AND availability_time <= :as_of
        ORDER BY security_id, effective_date
    """
    rows = connection.execute(
        text(sql),
        {"security_ids": [str(value) for value in security_ids], "as_of": as_of},
    ).all()
    return pd.DataFrame(rows, columns=empty_columns)


def load_latest_fundamentals_as_of(
    connection: Connection,
    security_ids: list[UUID],
    statement_type: CanonicalStatementType,
    as_of: datetime,
) -> pd.DataFrame:
    """The freshest knowable statement per security, in one query.

    The bulk equivalent of `get_latest_fundamental_as_of`: `DISTINCT ON
    (security_id)` ordered by `fiscal_period_end DESC, availability_time
    DESC` after the availability filter gives, per security, the most
    recently *ended* period that was knowable, and within that period its
    most recently knowable restatement.
    """
    columns = [
        "security_id",
        "fiscal_period_end",
        "observation_time",
        "availability_time",
        "data",
    ]
    if not security_ids:
        return pd.DataFrame(columns=columns)

    sql = """
        SELECT DISTINCT ON (security_id)
               security_id, fiscal_period_end, observation_time, availability_time, data
        FROM canonical_fundamentals
        WHERE security_id = ANY(:security_ids)
          AND statement_type = :statement_type
          AND availability_time <= :as_of
        ORDER BY security_id, fiscal_period_end DESC, availability_time DESC
    """
    rows = connection.execute(
        text(sql),
        {
            "security_ids": [str(value) for value in security_ids],
            "statement_type": statement_type.value,
            "as_of": as_of,
        },
    ).all()
    return pd.DataFrame(rows, columns=columns)
