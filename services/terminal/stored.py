"""PIT-correct reads over the Ultimate-plan tables. Nothing computed.

The four topic modules beside this one — `analyst.py`, `governance.py`,
`related.py`, `indicators.py` — all ask the same three questions of
different tables, so the queries live here once rather than four times.

## The restatement rule, and why it is not obvious

`canonical_disclosures` holds one row per *observation* of a period, not
one per period: an analyst estimate revised in June is a second row for
the same fiscal year. So "the latest estimate for FY2027 knowable in
July" is the row with the greatest `observation_time` **among rows for
that period whose `availability_time` has passed** — not simply the most
recently available row.

Getting that backwards produces exactly the bug
`core/data_validation/fundamentals.py` documents for statements: order on
availability alone and a query in October returns September's revision of
Q1 rather than the current Q2 — the wrong period, not a stale one. This
module borrows that reasoning rather than the code, because the table is
different, and states it here so the next reader does not have to
rediscover it.

## Absence is a first-class answer

Every reader returns `Unavailable` with a reason rather than an empty
object, matching `company.py`'s `_MISS_EXPLANATIONS`. Today the honest
reason is almost always the same one: the Ultimate plan's data has never
been ingested, so the endpoints report that plainly instead of looking
broken.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.engine import Connection

from core.data_validation.result import MissReason
from data.canonical_model.records import CanonicalDisclosureType, CanonicalSnapshotType
from infra.db.schema.terminal_data import (
    analyst_grades,
    canonical_disclosures,
    canonical_snapshots,
    technical_indicators,
)
from services.terminal.schemas import Unavailable

__all__ = [
    "MISS_EXPLANATIONS",
    "grade_actions",
    "indicator_series",
    "latest_disclosures",
    "latest_snapshot",
    "unavailable",
]

MISS_EXPLANATIONS: dict[MissReason, str] = {
    MissReason.NEVER_INGESTED: (
        "ARGUS has never held this kind of record for this security. It comes from "
        "an FMP Ultimate endpoint, and either that data has not been ingested yet or "
        "the provider does not cover this security."
    ),
    MissReason.NOT_YET_AVAILABLE: (
        "ARGUS holds this kind of record for this security, but none of it was "
        "knowable at the requested instant. A later cutoff would show it."
    ),
}


def unavailable(reason: MissReason) -> Unavailable:
    """The absence, named. Never a null and never an empty object."""
    return Unavailable(
        reason=reason.value,
        explanation=MISS_EXPLANATIONS.get(
            reason, "ARGUS has no such record knowable at this instant."
        ),
    )


def latest_disclosures(
    connection: Connection,
    security_id: UUID,
    disclosure_type: CanonicalDisclosureType,
    *,
    as_of: datetime,
    limit: int,
) -> tuple[list[Any], MissReason | None]:
    """The newest knowable observation of each period, newest period first.

    One row per fiscal period — the restatement rule in the module
    docstring — and `limit` periods of them, because an estimate series
    covers several forward years and a compensation history covers
    several past ones.

    The second element distinguishes the two kinds of empty: `None` rows
    with `NEVER_INGESTED` means ARGUS holds nothing for this security at
    all, while `NOT_YET_AVAILABLE` means it holds something none of which
    was knowable at `as_of`. Module 19's news reader draws the same
    distinction and for the same reason: they look identical and mean
    different things.
    """
    knowable = (
        select(
            canonical_disclosures.c.fiscal_period,
            func.max(canonical_disclosures.c.observation_time).label("latest"),
        )
        .where(
            canonical_disclosures.c.security_id == security_id,
            canonical_disclosures.c.disclosure_type == disclosure_type.value,
            canonical_disclosures.c.availability_time <= as_of,
        )
        .group_by(canonical_disclosures.c.fiscal_period)
        .subquery()
    )

    rows = connection.execute(
        select(canonical_disclosures)
        .join(
            knowable,
            (canonical_disclosures.c.fiscal_period == knowable.c.fiscal_period)
            & (canonical_disclosures.c.observation_time == knowable.c.latest),
        )
        .where(
            canonical_disclosures.c.security_id == security_id,
            canonical_disclosures.c.disclosure_type == disclosure_type.value,
        )
        .order_by(desc(canonical_disclosures.c.fiscal_period))
        .limit(limit)
    ).all()

    if rows:
        return list(rows), None
    return [], _why_empty(
        connection,
        canonical_disclosures,
        security_id,
        canonical_disclosures.c.disclosure_type == disclosure_type.value,
    )


def latest_snapshot(
    connection: Connection,
    security_id: UUID,
    snapshot_type: CanonicalSnapshotType,
    *,
    as_of: datetime,
) -> tuple[Any | None, MissReason | None]:
    """The most recent knowable observation of a rolling state."""
    row = connection.execute(
        select(canonical_snapshots)
        .where(
            canonical_snapshots.c.security_id == security_id,
            canonical_snapshots.c.snapshot_type == snapshot_type.value,
            canonical_snapshots.c.availability_time <= as_of,
        )
        .order_by(desc(canonical_snapshots.c.observation_time))
        .limit(1)
    ).one_or_none()

    if row is not None:
        return row, None
    return None, _why_empty(
        connection,
        canonical_snapshots,
        security_id,
        canonical_snapshots.c.snapshot_type == snapshot_type.value,
    )


def grade_actions(
    connection: Connection,
    security_id: UUID,
    *,
    as_of: datetime,
    limit: int,
) -> tuple[list[Any], MissReason | None]:
    """Rating actions, newest first.

    An event stream rather than a state, so every action is returned
    rather than only the latest: "who moved this week" is the question,
    and collapsing it to a current rating would answer a different one.
    """
    rows = connection.execute(
        select(analyst_grades)
        .where(
            analyst_grades.c.security_id == security_id,
            analyst_grades.c.availability_time <= as_of,
        )
        .order_by(desc(analyst_grades.c.event_time), desc(analyst_grades.c.id))
        .limit(limit)
    ).all()

    if rows:
        return list(rows), None
    return [], _why_empty(connection, analyst_grades, security_id, None)


def indicator_series(
    connection: Connection,
    security_id: UUID,
    *,
    indicator: str,
    period_length: int,
    timeframe: str,
    as_of: datetime,
    limit: int,
) -> tuple[list[Any], MissReason | None]:
    """One indicator's series, newest bar first.

    Keyed on all three parameters: a 14-period RSI and a 50-period RSI
    are different series, and returning one for the other would be a
    quietly wrong chart.
    """
    matches = (
        (technical_indicators.c.indicator == indicator)
        & (technical_indicators.c.period_length == period_length)
        & (technical_indicators.c.timeframe == timeframe)
    )
    rows = connection.execute(
        select(technical_indicators)
        .where(
            technical_indicators.c.security_id == security_id,
            matches,
            technical_indicators.c.availability_time <= as_of,
        )
        .order_by(desc(technical_indicators.c.event_time))
        .limit(limit)
    ).all()

    if rows:
        return list(rows), None
    return [], _why_empty(connection, technical_indicators, security_id, matches)


def _why_empty(
    connection: Connection,
    table: Any,
    security_id: UUID,
    extra: Any,
) -> MissReason:
    """Which kind of empty this is.

    Asked without the availability filter on purpose — the question is
    "does ARGUS carry this at all for this security", which is not the
    same question as "is any of it visible right now".
    """
    conditions = [table.c.security_id == security_id]
    if extra is not None:
        conditions.append(extra)

    held = connection.execute(
        select(func.count()).select_from(table).where(*conditions).limit(1)
    ).scalar_one()
    return MissReason.NOT_YET_AVAILABLE if held else MissReason.NEVER_INGESTED
