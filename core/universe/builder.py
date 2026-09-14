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

from collections.abc import Sequence
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
    seed_version_label,
)
from data.canonical_model.exchanges import CanonicalExchange, normalize_exchange, normalize_symbol
from data.normalization.identity import SecurityIdentityResolver
from data.provider_adapters.fmp.errors import FmpError
from data.provider_adapters.fmp.fetchers import FmpFetcher
from data.provider_adapters.fmp.models import DelistedSecurity, SecurityListing
from infra.db.schema.canonical import canonical_ohlcv

#: The venues ARGUS's universe is drawn from.
UNIVERSE_EXCHANGE_NAMES: tuple[str, ...] = ("NYSE", "NASDAQ")


@dataclass(slots=True)
class SeedReport:
    """What a seeded construction asked for, and what the provider gave back.

    Present only on the seed path, and its presence is what marks a
    construction as seeded everywhere downstream — the label, the stored
    definition and the build log all key off it rather than off a flag
    somebody has to remember to pass twice.
    """

    #: Symbols the operator asked for, in the order given.
    requested: tuple[str, ...] = ()
    #: Symbols the provider returned a profile row for.
    profiled: list[str] = field(default_factory=list)
    #: Symbols the provider knows nothing about — a typo, a delisted
    #: ticker, or a venue FMP does not carry. Named rather than counted:
    #: with twenty hand-typed symbols, *which* one was wrong is the whole
    #: question.
    not_found: list[str] = field(default_factory=list)
    #: Symbols whose fetch failed, by symbol, with the failure named. One
    #: bad symbol must not cost the other nineteen.
    failed: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "seed_requested": len(self.requested),
            "seed_symbols": list(self.requested),
            "seed_profiled": len(self.profiled),
            "seed_not_found": sorted(self.not_found),
            "seed_failed": dict(sorted(self.failed.items())),
            # Stated in every summary rather than implied by its absence:
            # this is the evidence difference between a seeded universe
            # and a real one, and it is the kind of caveat that gets lost
            # the moment it is only written in a docstring.
            "seed_delisted_sweep": False,
        }


@dataclass(slots=True)
class UniverseConstruction:
    """The result of building a universe: intervals plus what was excluded."""

    intervals: list[ListingInterval] = field(default_factory=list)
    admission: AdmissionReport = field(default_factory=AdmissionReport)
    #: When the provider responses were observed. Becomes the fallback
    #: `listed_from` for securities with no other evidence.
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Set only by `build_intervals_from_symbols`. None means this is a
    #: real, whole-market construction.
    seed: SeedReport | None = None

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


async def build_intervals_from_symbols(
    fetcher: FmpFetcher,
    resolver: SecurityIdentityResolver,
    symbols: Sequence[str],
    *,
    observed_at: datetime | None = None,
    first_bar_dates: dict[UUID, date] | None = None,
    last_bar_dates: dict[UUID, date] | None = None,
    register_unknown: bool = True,
) -> UniverseConstruction:
    """Build intervals from an explicit symbol list, one profile per symbol.

    **This is a test fixture, not a universe.** It exists for one reason:
    `/stable/stock-list` is paywalled below FMP's paid tiers, so on a free
    key `build_intervals_from_fetch` cannot return a single row — which is
    how ARGUS spent its whole existence unable to ingest one real bar.
    `/stable/profile` is available on the free tier, so a hand-picked
    handful of symbols can be taken end to end for the first time.

    It is deliberately a *sibling* of `build_intervals_from_fetch` rather
    than an option on it. That function's docstring states the rule it
    keeps — "no per-ticker calls, no hardcoded symbols, no assumed count"
    — and that rule is right for the production path and stays true of it.
    Per-ticker calls and a hardcoded list are exactly what this does, so
    it says so in its name, in its report, and in the version label it
    ends up producing.

    **Two evidence caveats, both real, neither hidden:**

    *No delisted sweep.* `fetch_delisted_companies` is not called —
    it is very likely gated on the free tier too, and calling it would
    reintroduce the paywall this path exists to route around. So a symbol
    that is in fact delisted is dated from `IntervalEvidence.FIRST_OBSERVED`
    rather than from the delisted feed. `check_valid_asset_identity`
    already understands that weaker evidence and records it; `SeedReport`
    states it in every summary so nobody has to infer it.

    *No IPO date, though the payload carries one.* A profile row includes
    `ipoDate`, which would be better evidence than the observation
    instant. Wiring it in means changing `_observe_listing`, which the
    whole-market path shares, so it is kept in the record's `raw` and left
    unused here rather than changed underneath the path that matters.

    Everything else is the ordinary path: `_observe_listing` unchanged, so
    admission rules, exclusion reporting and identity registration behave
    exactly as they do for the whole market, and `build_intervals`
    unchanged after it.
    """
    observed_at = observed_at or datetime.now(UTC)
    report = SeedReport(requested=tuple(symbols))
    construction = UniverseConstruction(observed_at=observed_at, seed=report)

    observations: list[ListingObservation] = []
    for symbol in symbols:
        try:
            profile = await fetcher.fetch_company_profile(symbol)
        except FmpError as error:
            # Named and survived rather than raised. Twenty symbols typed
            # by hand will contain a mistake, and losing the other
            # nineteen to it is a worse outcome than a short universe.
            report.failed[symbol] = f"{type(error).__name__}: {error}"
            continue

        if not profile.records:
            report.not_found.append(symbol)
            continue

        for listing in profile.records:
            report.profiled.append(listing.symbol)
            observation = _observe_listing(
                listing, construction.admission, resolver, observed_at, register_unknown
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

    seed = construction.seed
    definition = {
        "exchanges": list(UNIVERSE_EXCHANGE_NAMES),
        "source": {
            "provider": "fmp",
            "endpoints": (
                ["company_profile"] if seed else ["stock_list", "delisted_companies"]
            ),
        },
        "observed_at": construction.observed_at.isoformat(),
        "membership_checksum": checksum,
        "member_count": len(members),
        # The admission summary rides with the version so a later reader
        # can see how many securities were excluded, and why, without
        # re-running the fetch. An unexplained universe shrink is
        # diagnosable from the stored record.
        "admission": construction.admission.summary(),
    }
    if seed is not None:
        # Recorded in the version itself, because a seeded universe is a
        # test fixture and every statistic computed over one is a
        # statement about twenty hand-picked names rather than about the
        # market. A reader six months from now meets this row, not the
        # command that produced it.
        definition["seed"] = seed.summary()

    label = (
        seed_version_label(as_of, checksum, len(seed.requested))
        if seed is not None
        else default_version_label(as_of, checksum)
    )

    version_id = repository.create_version(
        version_label=label,
        as_of=as_of,
        definition=definition,
        description=description,
    )
    repository.add_members(version_id, members)

    return StoredUniverseVersion(
        id=version_id,
        version_label=label,
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
