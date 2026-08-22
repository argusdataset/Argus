"""Listing intervals — the dated join that makes the universe historical.

The point of this module in one sentence: **"was this security on
NYSE/NASDAQ on date X" has to be answerable for any X**, not just today.
If membership were a current-status flag, every historical backtest would
silently exclude every company that did not survive to the present, and
the resulting performance inflation would be invisible unless someone
went looking for it.

So each security gets one or more half-open `[listed_from, listed_to)`
intervals, and universe membership for a date is "which securities had an
interval covering it".

## Where the boundaries come from, and what FMP does not give us

**FMP's `stock-list` endpoint reports no listing or IPO date.** It says
what is listed *now* and nothing about when that started. The delisted
feed does carry `ipoDate` and `delistedDate`, so — counterintuitively —
ARGUS knows more about when a dead company was listed than about a live
one.

Boundaries are therefore established from the best evidence available,
and which evidence was used is recorded on every interval:

| Evidence | `listed_from` | Reliability |
|---|---|---|
| `DELISTED_FEED` | provider's `ipoDate` | reported |
| `PRICE_HISTORY` | first canonical bar | strong inference |
| `TICKER_HISTORY` | Module 05's `valid_from` | weak — when ARGUS first recorded it |
| `FIRST_OBSERVED` | when the listing was first seen | weakest |

An inferred boundary must stay distinguishable from a reported one.
Treating them as equally authoritative is how a "the universe says it was
listed" claim quietly becomes unfalsifiable.

## Erring direction

Where a boundary is unknown, intervals are made **narrower, not wider** —
ARGUS claims listing only over the span it can evidence. A too-wide
interval puts a security into backtests during periods it was not
tradeable, which is a silent correctness error. A too-narrow one omits it
from some periods, which is a visible coverage gap that shows up in the
construction report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from uuid import UUID

from data.canonical_model.exchanges import CanonicalExchange
from infra.db.enums import ListingStatus


class IntervalEvidence(StrEnum):
    """How a listing interval's boundaries were established.

    Ordered loosely from strongest to weakest. Stored on every membership
    row so an inferred boundary is never mistaken for a reported one.
    """

    #: Provider reported the date directly (delisted feed's ipoDate /
    #: delistedDate).
    DELISTED_FEED = "delisted_feed"
    #: Inferred from the earliest/latest canonical bar ARGUS holds. Strong:
    #: a security that traded on a date was listed on it.
    PRICE_HISTORY = "price_history"
    #: Module 05's security_ticker_history validity window. Weak — it
    #: records when ARGUS learned of the ticker, not when trading began.
    TICKER_HISTORY = "ticker_history"
    #: The moment the listing was first observed in a provider response.
    #: Weakest, and the fallback of last resort.
    FIRST_OBSERVED = "first_observed"
    #: A boundary the provider should have supplied but did not.
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class ListingObservation:
    """A security seen in a provider listing or delisting response.

    Provider-neutral: Module 04's records are reduced to this before any
    interval logic runs, so a second provider adapter changes only the
    reduction.
    """

    security_id: UUID
    symbol: str
    exchange: CanonicalExchange
    name: str | None = None
    #: From the delisted feed only; None for currently-listed securities.
    ipo_date: date | None = None
    delisted_date: date | None = None
    #: True when this came from the delisted feed rather than stock-list.
    is_delisted: bool = False


@dataclass(frozen=True, slots=True)
class ListingInterval:
    """A span during which a security was listed on a given venue.

    Half-open: `listed_from` is inclusive, `listed_to` exclusive. NULL
    `listed_to` means "still listed as far as ARGUS can tell".
    """

    security_id: UUID
    symbol: str
    exchange: CanonicalExchange
    listed_from: datetime
    listed_to: datetime | None
    listing_status: ListingStatus
    from_evidence: IntervalEvidence
    to_evidence: IntervalEvidence

    def covers(self, moment: datetime) -> bool:
        """Whether this interval includes `moment`."""
        if moment < self.listed_from:
            return False
        return self.listed_to is None or moment < self.listed_to

    @property
    def evidence_label(self) -> str:
        """Compact record of how both boundaries were established."""
        return f"from={self.from_evidence.value};to={self.to_evidence.value}"


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def build_intervals(
    observations: list[ListingObservation],
    *,
    observed_at: datetime,
    first_bar_dates: dict[UUID, date] | None = None,
    last_bar_dates: dict[UUID, date] | None = None,
) -> list[ListingInterval]:
    """Turn listing observations into dated intervals, joined by identity.

    Joining on `security_id` rather than symbol is what makes a ticker
    change (Module 05's FB -> META) produce **one** membership record
    instead of two disconnected ones.

    `first_bar_dates` / `last_bar_dates` supply price-history evidence
    where available. They are passed in rather than queried here so the
    interval logic stays pure and testable, and so a caller can skip the
    lookup entirely when price data has not been ingested yet.

    Handles the relisting case: a security appearing in *both* the
    delisted feed and current listings gets two intervals — the historical
    one the feed describes, and a current one starting from the earliest
    date ARGUS can evidence trading resumed. It deliberately does not
    bridge the gap between them, because claiming continuous listing
    across a period the security was not tradeable would be a silent
    error, while the gap itself is visible.
    """
    first_bar_dates = first_bar_dates or {}
    last_bar_dates = last_bar_dates or {}

    by_security: dict[UUID, list[ListingObservation]] = {}
    for observation in observations:
        by_security.setdefault(observation.security_id, []).append(observation)

    intervals: list[ListingInterval] = []
    for security_id, group in by_security.items():
        delistings = [item for item in group if item.is_delisted]
        current = [item for item in group if not item.is_delisted]

        for delisting in delistings:
            intervals.append(
                _delisted_interval(
                    delisting,
                    observed_at=observed_at,
                    first_bar=first_bar_dates.get(security_id),
                    last_bar=last_bar_dates.get(security_id),
                )
            )

        for listing in current:
            intervals.append(
                _current_interval(
                    listing,
                    observed_at=observed_at,
                    first_bar=first_bar_dates.get(security_id),
                    # A security in both feeds relisted after the latest
                    # delisting; its current interval cannot start before
                    # that.
                    relisted_after=max(
                        (item.delisted_date for item in delistings if item.delisted_date),
                        default=None,
                    ),
                )
            )

    intervals.sort(key=lambda item: (item.symbol, item.listed_from))
    return intervals


def _delisted_interval(
    observation: ListingObservation,
    *,
    observed_at: datetime,
    first_bar: date | None,
    last_bar: date | None,
) -> ListingInterval:
    """Interval for a security the provider reports as delisted."""
    if observation.ipo_date is not None:
        listed_from, from_evidence = (
            _start_of_day(observation.ipo_date),
            IntervalEvidence.DELISTED_FEED,
        )
    elif first_bar is not None:
        listed_from, from_evidence = _start_of_day(first_bar), IntervalEvidence.PRICE_HISTORY
    else:
        # No listing date and no price history. The only defensible claim
        # is the delisting itself, so the interval collapses to the day
        # before it — narrow rather than wide, and flagged as missing.
        anchor = observation.delisted_date or observed_at.date()
        listed_from, from_evidence = _start_of_day(anchor), IntervalEvidence.MISSING

    if observation.delisted_date is not None:
        listed_to, to_evidence = (
            _start_of_day(observation.delisted_date),
            IntervalEvidence.DELISTED_FEED,
        )
    elif last_bar is not None:
        # Known to have left, but not when. The last bar is the last date
        # trading can be evidenced.
        listed_to, to_evidence = _start_of_day(last_bar), IntervalEvidence.PRICE_HISTORY
    else:
        listed_to, to_evidence = observed_at, IntervalEvidence.MISSING

    # A degenerate interval would violate the database's ordering check.
    # Widening by a day is the minimum that keeps the record storable, and
    # the MISSING evidence marker says not to trust the boundary.
    if listed_to <= listed_from:
        listed_from = listed_to - _ONE_DAY

    return ListingInterval(
        security_id=observation.security_id,
        symbol=observation.symbol,
        exchange=observation.exchange,
        listed_from=listed_from,
        listed_to=listed_to,
        listing_status=ListingStatus.DELISTED,
        from_evidence=from_evidence,
        to_evidence=to_evidence,
    )


def _current_interval(
    observation: ListingObservation,
    *,
    observed_at: datetime,
    first_bar: date | None,
    relisted_after: date | None,
) -> ListingInterval:
    """Interval for a security the provider reports as currently listed.

    `listed_to` is always None: the security is listed as far as ARGUS can
    tell, and inventing an end date would be a fabrication.
    """
    if first_bar is not None:
        listed_from, from_evidence = _start_of_day(first_bar), IntervalEvidence.PRICE_HISTORY
    else:
        # FMP's stock-list carries no IPO date, so with no price history
        # the earliest defensible claim is "listed when we first saw it".
        listed_from, from_evidence = observed_at, IntervalEvidence.FIRST_OBSERVED

    if relisted_after is not None:
        # Relisting: the current interval cannot begin before the security
        # came back. The gap between delisting and relisting stays a gap.
        floor = _start_of_day(relisted_after)
        if listed_from < floor:
            listed_from, from_evidence = floor, IntervalEvidence.MISSING

    return ListingInterval(
        security_id=observation.security_id,
        symbol=observation.symbol,
        exchange=observation.exchange,
        listed_from=listed_from,
        listed_to=None,
        listing_status=ListingStatus.LISTED,
        from_evidence=from_evidence,
        to_evidence=IntervalEvidence.MISSING,
    )


def intervals_covering(
    intervals: list[ListingInterval], moment: datetime
) -> dict[UUID, ListingInterval]:
    """The interval per security that covers `moment`.

    Keyed by `security_id` because `universe_membership` permits one row
    per security per version. Where a security somehow has overlapping
    intervals, the one starting latest wins — the most recent evidence is
    the most likely to describe the security's state at that moment.
    """
    covering: dict[UUID, ListingInterval] = {}
    for interval in intervals:
        if not interval.covers(moment):
            continue
        existing = covering.get(interval.security_id)
        if existing is None or interval.listed_from > existing.listed_from:
            covering[interval.security_id] = interval
    return covering


_ONE_DAY = datetime(2000, 1, 2, tzinfo=UTC) - datetime(2000, 1, 1, tzinfo=UTC)
