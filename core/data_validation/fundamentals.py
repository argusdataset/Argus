"""PIT-safe access to canonical fundamentals.

The most important entity in this module: fundamentals is where the
leakage risk Module 05 identified — `observation_time` from `acceptedDate`,
never fiscal period end — actually gets tested against a query that could
undo it.

## What identifies "the same logical record" across restatements

Not `fiscal_period` (a label like `"Q1"`) alone. FMP does not year-qualify
that field, and Module 03's uniqueness constraint on
`canonical_fundamentals` is `(security_id, statement_type, fiscal_period,
observation_time)` — no `fiscal_period_end`. In practice a same-second
collision between two different years' "Q1" filings is astronomically
unlikely, so the constraint still enforces real uniqueness; but it means
`fiscal_period` is a *label*, not the thing that actually identifies a
period. `fiscal_period_end` — a real date — is what does. Every function
here keys a specific period by `(security_id, statement_type,
fiscal_period_end)`, never by the label alone, precisely so a 2019 Q1
restatement can never be read as a later observation of 2020 Q1.

This was caught while building this module rather than found broken by a
test; it doesn't need a migration, because the ambiguity is a latent
labelling weakness with no practical collision path, not a constraint
that actually admits wrong data. It does need the query layer to use the
correct column, which is what this module now does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.data_validation.result import AsOfResult, MissReason
from data.canonical_model.records import CanonicalStatementType
from infra.db.schema.canonical import canonical_fundamentals


@dataclass(frozen=True, slots=True)
class FundamentalAsOf:
    """One statement, as it was known at query time."""

    security_id: UUID
    statement_type: CanonicalStatementType
    fiscal_period: str
    fiscal_period_end: date
    observation_time: datetime
    availability_time: datetime
    data: dict[str, Any]


def _row_to_result(row: Any, as_of: datetime) -> AsOfResult[FundamentalAsOf]:
    fiscal_period_end = row.fiscal_period_end
    if isinstance(fiscal_period_end, datetime):
        fiscal_period_end = fiscal_period_end.date()
    return AsOfResult.hit(
        FundamentalAsOf(
            security_id=row.security_id,
            statement_type=CanonicalStatementType(row.statement_type),
            fiscal_period=row.fiscal_period,
            fiscal_period_end=fiscal_period_end,
            observation_time=row.observation_time,
            availability_time=row.availability_time,
            data=dict(row.data),
        ),
        as_of=as_of,
    )


def get_fundamental_as_of(
    connection: Connection,
    security_id: UUID,
    statement_type: CanonicalStatementType,
    fiscal_period_end: date,
    as_of: datetime,
) -> AsOfResult[FundamentalAsOf]:
    """The statement for one specific fiscal period, as knowable at `as_of`.

    Selects the row with the greatest `availability_time <= as_of` among
    every restatement of that period — never the first filed, never
    whatever the current value happens to be, always what was actually
    knowable on `as_of`. This is the query the adversarial leakage test
    in `tests/unit/data_validation/test_pit_enforcement.py` exercises
    directly.
    """
    query = (
        select(canonical_fundamentals)
        .where(
            canonical_fundamentals.c.security_id == security_id,
            canonical_fundamentals.c.statement_type == statement_type.value,
            canonical_fundamentals.c.fiscal_period_end == fiscal_period_end,
            canonical_fundamentals.c.availability_time <= as_of,
        )
        .order_by(canonical_fundamentals.c.availability_time.desc())
        .limit(1)
    )
    row = connection.execute(query).first()
    if row is None:
        return AsOfResult.miss(MissReason.NOT_YET_AVAILABLE, as_of=as_of)
    return _row_to_result(row, as_of)


def get_latest_fundamental_as_of(
    connection: Connection,
    security_id: UUID,
    statement_type: CanonicalStatementType,
    as_of: datetime,
) -> AsOfResult[FundamentalAsOf]:
    """The most recently-ended period whose value was knowable by `as_of`.

    The query feature engineering actually needs most often: "what is the
    freshest fundamentals figure ARGUS could have used for this security
    on this date", not "give me a specific quarter". Ordering by
    `fiscal_period_end DESC, availability_time DESC` after the
    `availability_time <= as_of` filter is sufficient on its own to get
    both the most-recently-known period *and* its most-recently-known
    restatement correctly — no separate group-by step is needed, because
    a period that is not yet knowable is excluded by the filter before
    the ordering ever sees it.
    """
    query = (
        select(canonical_fundamentals)
        .where(
            canonical_fundamentals.c.security_id == security_id,
            canonical_fundamentals.c.statement_type == statement_type.value,
            canonical_fundamentals.c.availability_time <= as_of,
        )
        .order_by(
            canonical_fundamentals.c.fiscal_period_end.desc(),
            canonical_fundamentals.c.availability_time.desc(),
        )
        .limit(1)
    )
    row = connection.execute(query).first()
    if row is None:
        return AsOfResult.miss(MissReason.NOT_YET_AVAILABLE, as_of=as_of)
    return _row_to_result(row, as_of)
