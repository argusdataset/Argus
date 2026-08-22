"""Constructing a universe version for any date.

The whole module exists so this question is answerable for any date, not
just today:

    Which securities existed and were tradeable on NYSE/NASDAQ as of X?

Construction runs in two separable halves. `build_intervals_from_fetch`
turns provider responses into dated listing intervals — pure, no database
writes. `construct_version` then takes those intervals plus an as-of date
and persists the version. The split matters practically: intervals are
expensive to build (a full fetch) and cheap to re-slice, so one fetch can
produce a version for 2015, one for 2020, and one for today without
refetching.

**Historical versions include securities that have since been delisted.**
That is the point. A universe built only from today's listings excludes
every company that did not survive, and the resulting inflation in
backtested performance is invisible unless someone specifically checks
for it. So the delisted feed is fetched alongside current listings and
both contribute intervals.

This module does not decide *why* a security left the universe. FMP does
not report a delisting reason (Module 04's finding), and inventing a
taxonomy here would be a guess dressed as data. Module 09's
bankruptcy-exclusion gate needs that question answered; it needs its own
approach.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from core.universe.admission import AdmissionReport, admits
from core.universe.intervals import (
    ListingInterval,
    ListingObservation,
    build_intervals,
    intervals_covering,
)
from core.universe.repository import (
    StoredUniverseVersion,
    UniverseRepository,
    default_version_label,
    membership_checksum,
)
from data.canonical_model.exchanges import CanonicalExchange, normalize_exchange, normalize_symbol
from data.normalization.identity import SecurityIdentityResolver
from data.provider_adapters.fmp.fetchers import FmpFetcher
from data.provider_adapters.fmp.models import DelistedSecurity, SecurityListing
from infra.db.schema.canonical import canonical_ohlcv

#: The venues ARGUS's universe is drawn from.
UNIVERSE_EXCHANGE_NAMES: tuple[str, ...] = ("NYSE", "NASDAQ")


@dataclass(slots=True)
class UniverseConstruction:
    """The result of building a universe: intervals plus what was excluded."""

    intervals: list[ListingInterval] = field(default_factory=list)
    admission: AdmissionReport = field(default_factory=AdmissionReport)
    #: When the provider responses were observed. Becomes the fallback
    #: `listed_from` for securities with no other evidence.
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def security_count(self) -> int:
        return len({interval.security_id for interval in self.intervals})


async def build_intervals_from_fetch(
    fetcher: FmpFetcher,
    resolver: SecurityIdentityResolver,
    *,
    observed_at: datetime | None = None,
    first_bar_dates: dict[UUID, date] | None = None,
    last_bar_dates: dict[UUID, date] | None = None,
    register_unknown: bool = True,
) -> UniverseConstruction:
    """Fetch listings and delistings, and reduce them to dated intervals.

    One `stock-list` call covers the whole universe (Module 04's finding),
    plus a paged `delisted-companies` sweep. No per-ticker calls, no
    hardcoded symbols, no assumed count.

    `register_unknown` controls whether a symbol with no existing identity
    gets one minted via Module 05's resolver. Registration lives in Module
    05; this module only asks it to run, and only for securities that pass
    admission — there is no reason to mint identities for venues ARGUS
    does not cover.
    """
    observed_at = observed_at or datetime.now(UTC)
    construction = UniverseConstruction(observed_at=observed_at)

    # The UNFILTERED list, deliberately. Module 04's
    # `fetch_exchange_listings` applies its own client-side exchange
    # filter, which would discard the very rows this module has to
    # count — an unrecognised venue label would be dropped there and
    # never appear in the admission report, which is exactly the silent
    # universe shrink the report exists to catch. Admission is this
    # module's decision, so it needs to see everything.
    listings = await fetcher.fetch_stock_list()
    delisted = await fetcher.fetch_delisted_companies()

    observations: list[ListingObservation] = []
    for listing in listings.records:
        observation = _observe_listing(
            listing, construction.admission, resolver, observed_at, register_unknown
        )
        if observation is not None:
            observations.append(observation)

    for record in delisted.records:
        observation = _observe_delisting(
            record, construction.admission, resolver, observed_at, register_unknown
        )
        if observation is not None:
            observations.append(observation)

    construction.intervals = build_intervals(
        observations,
        observed_at=observed_at,
        first_bar_dates=first_bar_dates,
        last_bar_dates=last_bar_dates,
    )
    return construction


def _classify(
    symbol_raw: str | None,
    exchange_raw: str | None,
    exchange_short: str | None,
    report: AdmissionReport,
) -> tuple[str, CanonicalExchange] | None:
    """Normalize and admission-check one provider row.

    Returns None when the security is excluded — always after recording
    the reason, never silently.
    """
    symbol = normalize_symbol(symbol_raw or "")
    exchange = normalize_exchange(exchange_raw, exchange_short)

    reason = admits(exchange, symbol)
    if reason is not None:
        report.exclude(symbol, reason, exchange_label=exchange_raw or exchange_short)
        return None

    report.admit(exchange)
    return symbol, exchange


def _observe_listing(
    listing: SecurityListing,
    report: AdmissionReport,
    resolver: SecurityIdentityResolver,
    observed_at: datetime,
    register_unknown: bool,
) -> ListingObservation | None:
    classified = _classify(listing.symbol, listing.exchange, listing.exchange_short_name, report)
    if classified is None:
        return None
    symbol, exchange = classified

    security_id = _identity(resolver, symbol, exchange, observed_at, register_unknown, report)
    if security_id is None:
        return None

    return ListingObservation(
        security_id=security_id,
        symbol=symbol,
        exchange=exchange,
        name=listing.name,
        is_delisted=False,
    )


def _observe_delisting(
    record: DelistedSecurity,
    report: AdmissionReport,
    resolver: SecurityIdentityResolver,
    observed_at: datetime,
    register_unknown: bool,
) -> ListingObservation | None:
    # The delisted feed carries only one exchange label, so both
    # normalize_exchange arguments get it.
    classified = _classify(record.symbol, record.exchange, record.exchange, report)
    if classified is None:
        return None
    symbol, exchange = classified

    # A delisted security's identity is registered from its IPO date where
    # known, so its ticker-history window covers the era it traded in.
    valid_from = (
        datetime.combine(record.ipo_date, datetime.min.time(), tzinfo=UTC)
        if record.ipo_date
        else observed_at
    )
    security_id = _identity(resolver, symbol, exchange, valid_from, register_unknown, report)
    if security_id is None:
        return None

    return ListingObservation(
        security_id=security_id,
        symbol=symbol,
        exchange=exchange,
        name=record.company_name,
        ipo_date=record.ipo_date,
        delisted_date=record.delisted_date,
        is_delisted=True,
    )


def _identity(
    resolver: SecurityIdentityResolver,
    symbol: str,
    exchange: CanonicalExchange,
    valid_from: datetime,
    register_unknown: bool,
    report: AdmissionReport,
) -> UUID | None:
    """Resolve, optionally minting via Module 05. Never mints its own scheme."""
    from core.universe.admission import ExclusionReason

    existing = resolver.try_resolve(symbol)
    if existing is not None:
        return existing
    if not register_unknown:
        report.exclude(symbol, ExclusionReason.UNRESOLVED_IDENTITY)
        return None
    return resolver.register(symbol, exchange=exchange, valid_from=valid_from)


def construct_version(
    construction: UniverseConstruction,
    repository: UniverseRepository,
    *,
    as_of: datetime | None = None,
    description: str | None = None,
) -> StoredUniverseVersion:
    """Persist the universe as it stood on `as_of`.

    Members are the securities whose listing interval covers that moment,
    which for a historical date naturally includes companies delisted
    since — the survivorship-bias resistance this module exists for.

    An identical universe already recorded for the same date is reused
    rather than duplicated: versions are immutable and append-only, so
    writing a second copy of the same membership would just make the
    history harder to read.
    """
    as_of = as_of or construction.observed_at
    covering = intervals_covering(construction.intervals, as_of)
    members = sorted(covering.values(), key=lambda interval: interval.symbol)

    checksum = membership_checksum(members)
    existing = repository.find_equivalent(as_of, checksum)
    if existing is not None:
        return existing

    definition = {
        "exchanges": list(UNIVERSE_EXCHANGE_NAMES),
        "source": {"provider": "fmp", "endpoints": ["stock_list", "delisted_companies"]},
        "observed_at": construction.observed_at.isoformat(),
        "membership_checksum": checksum,
        "member_count": len(members),
        # The admission summary rides with the version so a later reader
        # can see how many securities were excluded, and why, without
        # re-running the fetch. An unexplained universe shrink is
        # diagnosable from the stored record.
        "admission": construction.admission.summary(),
    }

    version_id = repository.create_version(
        version_label=default_version_label(as_of, checksum),
        as_of=as_of,
        definition=definition,
        description=description,
    )
    repository.add_members(version_id, members)

    return StoredUniverseVersion(
        id=version_id,
        version_label=default_version_label(as_of, checksum),
        as_of_date=as_of,
        member_count=len(members),
        reused=False,
    )


def bar_date_bounds(
    connection: Connection, security_ids: list[UUID] | None = None
) -> tuple[dict[UUID, date], dict[UUID, date]]:
    """Earliest and latest canonical bar date per security.

    This is the price-history evidence `build_intervals` uses to date a
    listing. It matters because FMP's `stock-list` carries no IPO date, so
    without it a currently-listed security's interval can only start when
    ARGUS first happened to look — which would exclude it from every
    earlier historical universe.

    Returns empty mappings when no bars have been ingested; interval
    construction degrades to weaker evidence rather than failing.
    """
    query = select(
        canonical_ohlcv.c.security_id,
        func.min(canonical_ohlcv.c.event_time).label("first_bar"),
        func.max(canonical_ohlcv.c.event_time).label("last_bar"),
    ).group_by(canonical_ohlcv.c.security_id)

    if security_ids:
        query = query.where(canonical_ohlcv.c.security_id.in_(security_ids))

    first: dict[UUID, date] = {}
    last: dict[UUID, date] = {}
    for row in connection.execute(query):
        first[row.security_id] = row.first_bar.date()
        last[row.security_id] = row.last_bar.date()
    return first, last
