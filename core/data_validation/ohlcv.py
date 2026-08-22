"""PIT-safe access to canonical OHLCV bars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.engine import Connection

from core.data_validation.engine import select_latest_as_of
from core.data_validation.result import AsOfResult, MissReason
from data.canonical_model.pit import session_close
from data.canonical_model.records import CanonicalTimeframe
from infra.db.schema.canonical import canonical_ohlcv


@dataclass(frozen=True, slots=True)
class OhlcvBarAsOf:
    """One bar, as it was known at query time."""

    security_id: UUID
    timeframe: CanonicalTimeframe
    event_time: datetime
    observation_time: datetime
    availability_time: datetime
    open_raw: Decimal
    high_raw: Decimal
    low_raw: Decimal
    close_raw: Decimal
    volume_raw: int
    open_adjusted: Decimal | None
    high_adjusted: Decimal | None
    low_adjusted: Decimal | None
    close_adjusted: Decimal | None
    volume_adjusted: int | None


def get_ohlcv_bar_as_of(
    connection: Connection,
    security_id: UUID,
    bar_date: date,
    as_of: datetime,
    *,
    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY,
) -> AsOfResult[OhlcvBarAsOf]:
    """The bar for `bar_date`, as it was knowable at `as_of`.

    A provider correction to a bar is a restatement — a new row with a
    later `observation_time`/`availability_time` — so this selects the
    row with the greatest `availability_time <= as_of`, never simply the
    latest row for the date.
    """
    row = select_latest_as_of(
        connection,
        canonical_ohlcv,
        key={
            "security_id": security_id,
            "timeframe": timeframe.value,
            "event_time": session_close(bar_date),
        },
        as_of=as_of,
    )
    if row is None:
        return AsOfResult.miss(MissReason.NOT_YET_AVAILABLE, as_of=as_of)

    return AsOfResult.hit(
        OhlcvBarAsOf(
            security_id=row.security_id,
            timeframe=CanonicalTimeframe(row.timeframe),
            event_time=row.event_time,
            observation_time=row.observation_time,
            availability_time=row.availability_time,
            open_raw=row.open_raw,
            high_raw=row.high_raw,
            low_raw=row.low_raw,
            close_raw=row.close_raw,
            volume_raw=row.volume_raw,
            open_adjusted=row.open_adjusted,
            high_adjusted=row.high_adjusted,
            low_adjusted=row.low_adjusted,
            close_adjusted=row.close_adjusted,
            volume_adjusted=row.volume_adjusted,
        ),
        as_of=as_of,
    )
