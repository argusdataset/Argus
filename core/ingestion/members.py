"""Who to fetch for, and what is already held.

Two reads, both against tables other modules own.

## Universe membership is Module 06's, via Module 07's reader

`list_universe_members_as_of` is reused rather than re-derived. It
applies Module 06's half-open listing-interval predicate
(`listed_from <= as_of AND (listed_to IS NULL OR listed_to > as_of)`),
which is a different and for this entity correct mechanism from the
`availability_time` filter every other canonical table uses. Writing that
predicate a second time here would be a second place for it to be wrong.

## Membership gives identities; FMP needs tickers

`MembershipAsOf` carries `security_id` and no ticker, which is the whole
point of Module 05's identity discipline: nothing in ARGUS references a
security by ticker. But the provider only speaks tickers, so somewhere
has to translate, at the same `as_of` the membership was read at, so a
ticker change resolves to the holder on that date rather than to today's.

That translation used to be a private query here. It is now Module 07's
`tickers_as_of` — the same predicate three other places had also written
out privately. See `core/data_validation/identity.py` on why it moved
and which copies remain.

A member with no ticker valid at `as_of` is *reported*, not skipped
silently: it means a universe member ARGUS cannot fetch prices for, and
a run that quietly dropped it would show up later as unexplained missing
coverage.

## "Already held" is a storage question, not a point-in-time one

`securities_with_bars_on` deliberately does **not** filter on
`availability_time`. It is answering "is this row already in the
database", so that a re-run of the same day re-requests nothing — and a
PIT filter would give the wrong answer to that question whenever the
provider-lag policy puts a bar's `availability_time` past whatever cutoff
was passed in. That is not hypothetical: Module 05 derives a daily bar's
availability as the session close plus sixteen hours, and Module 18's
cutoff for the same date is the close plus five, so a PIT-filtered check
would call every bar it just wrote missing and fetch the whole universe
again. Whether a stored bar is *knowable* at a cutoff is Module 18's
question to ask, with Module 18's cutoff, and it asks it in
`readiness.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.data_validation.identity import tickers_as_of
from core.data_validation.universe import list_universe_members_as_of
from data.canonical_model.records import CanonicalTimeframe
from infra.db.schema.canonical import canonical_ohlcv

__all__ = ["MemberSet", "UniverseMember", "securities_with_bars_on", "universe_members"]


@dataclass(frozen=True, slots=True)
class UniverseMember:
    """One security to fetch for, with the ticker to fetch it under."""

    security_id: UUID
    ticker: str


@dataclass(frozen=True, slots=True)
class MemberSet:
    """The universe as this run sees it, including what it cannot fetch."""

    members: tuple[UniverseMember, ...]
    #: Members with no ticker valid at `as_of`. Reported, never dropped.
    unresolved: tuple[UUID, ...]

    @property
    def size(self) -> int:
        """Every member, fetchable or not — the denominator for coverage."""
        return len(self.members) + len(self.unresolved)

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(member.ticker for member in self.members)

    def by_ticker(self) -> dict[str, UUID]:
        return {member.ticker: member.security_id for member in self.members}


def universe_members(
    connection: Connection,
    *,
    universe_version_id: UUID,
    as_of: datetime,
) -> MemberSet:
    """Every universe member at `as_of`, paired with its ticker then."""
    memberships = list_universe_members_as_of(connection, universe_version_id, as_of)
    if not memberships:
        return MemberSet(members=(), unresolved=())

    identities = [membership.security_id for membership in memberships]
    tickers = tickers_as_of(connection, identities, as_of=as_of)

    members: list[UniverseMember] = []
    unresolved: list[UUID] = []
    for security_id in identities:
        ticker = tickers.get(security_id)
        if ticker is None:
            unresolved.append(security_id)
        else:
            members.append(UniverseMember(security_id=security_id, ticker=ticker))

    return MemberSet(members=tuple(members), unresolved=tuple(unresolved))


def securities_with_bars_on(
    connection: Connection,
    security_ids: list[UUID],
    *,
    trading_date: date,
    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY,
) -> set[UUID]:
    """Which of `security_ids` already hold a bar for `trading_date`.

    One aggregate rather than a per-security lookup: at ten thousand
    names asked once a day, the difference is one round trip against ten
    thousand.

    A bar's `event_time` is its session close, so bracketing the whole
    UTC day containing it selects exactly one session.
    """
    if not security_ids:
        return set()

    day_start = datetime.combine(trading_date, time.min, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)

    rows = connection.execute(
        select(canonical_ohlcv.c.security_id)
        .where(
            canonical_ohlcv.c.security_id.in_(security_ids),
            canonical_ohlcv.c.timeframe == timeframe.value,
            canonical_ohlcv.c.event_time >= day_start,
            canonical_ohlcv.c.event_time < day_end,
        )
        .distinct()
    ).scalars()
    return set(rows)
