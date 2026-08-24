"""The response contract, in Pydantic. This file *is* the API.

Every other module in ARGUS could be understood by reading the modules
around it. This one is read by a consumer that sees only these shapes —
v0's frontend now, a mobile client later — so the shapes have to explain
themselves.

Three conventions do most of that work, and they are the same three
everywhere in this file:

## 1. A missing value says why it is missing

Eighteen modules have enforced one rule harder than any other: `None` is
never `0.0`, and an absence is a different fact from a measurement. That
rule has to survive contact with JSON, where `null` means everything and
nothing.

So a block of data that could not be produced is not `null`. It is
present, marked `available: false`, and carries a `reason` naming which
of `MissReason`'s cases applied. A consumer rendering "—" versus "no data
yet" versus "not covered" can tell them apart without guessing.

## 2. Every point-in-time answer carries its own cutoff

`as_of` appears on every response derived from PIT data, and it is the
instant the answer was true as of — not the time the request was served.
Two calls a second apart return the same `as_of` if they name the same
one, which is what makes a Terminal screenshot reproducible.

## 3. Identity is a security, not a ticker

Every company response carries `security_id` alongside `ticker`. Tickers
are recycled; ARGUS's identity is not. A client that stores `security_id`
keeps working through a ticker change, and one that stores the ticker
does not. Saying so in the shape is cheaper than saying it in a document
nobody reads.

## Naming

`snake_case` throughout, including the TradingView datafeed responses
where the protocol dictates its own names (`s`, `t`, `o`, `h`, `l`, `c`,
`v`) — those are the library's contract, not ours, and renaming them
would break the widget.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "BarsResponse",
    "CompanyProfile",
    "DatafeedConfig",
    "FinancialStatement",
    "FundamentalsResponse",
    "NewsArticle",
    "NewsResponse",
    "ScanStatusResponse",
    "SymbolInfo",
    "SymbolSearchResult",
    "Unavailable",
    "ValuationResponse",
    "WatchlistDetail",
    "WatchlistItem",
    "WatchlistSummary",
]


class Unavailable(BaseModel):
    """Why a block of data could not be produced.

    Never `null` in its place. A consumer that sees this knows the request
    succeeded and ARGUS genuinely has nothing, and knows which kind of
    nothing — which is the difference between "not filed yet" and "we do
    not carry this".
    """

    available: Literal[False] = False
    #: One of Module 07's `MissReason` values, verbatim.
    reason: str
    #: Prose for a log or a tooltip. Never parsed.
    explanation: str


class CompanyProfile(BaseModel):
    """Who this is. Stable identity first, display name second."""

    security_id: UUID = Field(description="ARGUS's permanent identity. Survives ticker changes.")
    ticker: str = Field(description="The ticker as of `as_of`, not necessarily today's.")
    name: str | None = Field(default=None, description="Most recently known display name.")
    exchange: str | None = None
    as_of: datetime = Field(description="The instant this identity resolution was true as of.")


class FinancialStatement(BaseModel):
    """One statement for one fiscal period, exactly as Module 05 stored it.

    `data` is passed through unflattened and unrenamed. Module 05 owns the
    canonical field taxonomy; reshaping it here would create a second
    vocabulary for the same numbers and guarantee they eventually
    disagree.
    """

    statement_type: str
    fiscal_period: str
    fiscal_period_end: date
    #: When this became knowable to ARGUS. A consumer showing "reported"
    #: dates should use this, not `fiscal_period_end`.
    availability_time: datetime
    data: dict[str, Any]


class FundamentalsResponse(BaseModel):
    """Latest statements knowable at `as_of`, one per statement type.

    `statements` holds what was found; `unavailable` explains every type
    that was asked for and not found. The two together always cover the
    full set, so a consumer never has to guess whether a missing key means
    "not requested" or "not there".
    """

    security: CompanyProfile
    as_of: datetime
    statements: dict[str, FinancialStatement] = Field(default_factory=dict)
    unavailable: dict[str, Unavailable] = Field(default_factory=dict)


class ValuationResponse(BaseModel):
    """Valuation metrics, read from the stored KEY_METRICS / RATIOS statements.

    Nothing is computed here. ARGUS stores what the provider reported and
    this returns it — a P/E derived in this module would be a number no
    other part of the system could reproduce, and would have no PIT
    provenance at all.
    """

    security: CompanyProfile
    as_of: datetime
    metrics: dict[str, Any] = Field(default_factory=dict)
    #: Which stored statement each metric block came from.
    sources: dict[str, str] = Field(default_factory=dict)
    unavailable: dict[str, Unavailable] = Field(default_factory=dict)


class NewsArticle(BaseModel):
    """One article. Never scored, and nothing here implies otherwise."""

    headline: str
    published_at: datetime
    source_site: str | None = None
    url: str | None = None
    summary: str | None = None


class NewsResponse(BaseModel):
    security: CompanyProfile
    as_of: datetime
    articles: list[NewsArticle] = Field(default_factory=list)
    #: True when ARGUS has never ingested news for this security at all,
    #: as distinct from "has news, none of it knowable by `as_of`". Same
    #: distinction Module 18's report drew for scan availability, and for
    #: the same reason: an empty list is two different facts.
    ever_ingested: bool = False


class SymbolInfo(BaseModel):
    """TradingView `resolveSymbol` response.

    Field names are the Charting Library's, not ARGUS's. They look
    unlike the rest of this file on purpose — renaming them would break
    the widget.
    """

    name: str
    ticker: str
    description: str
    type: str = "stock"
    session: str = "0930-1600"
    timezone: str = "America/New_York"
    exchange: str
    listed_exchange: str
    minmov: int = 1
    pricescale: int = 100
    has_intraday: bool = False
    has_daily: bool = True
    has_weekly_and_monthly: bool = True
    supported_resolutions: list[str] = Field(default_factory=list)
    volume_precision: int = 0
    data_status: str = "endofday"


class SymbolSearchResult(BaseModel):
    """One row in TradingView's symbol search. Library field names again."""

    symbol: str
    full_name: str
    description: str
    exchange: str
    ticker: str
    type: str = "stock"


class BarsResponse(BaseModel):
    """TradingView UDF `/history` response.

    The single-letter names are the protocol's. `s` is the status:
    `"ok"`, `"no_data"`, or `"error"`. On `no_data` the arrays are empty
    and `next_time` may point at where data does exist.
    """

    s: str
    t: list[int] = Field(default_factory=list)
    o: list[float] = Field(default_factory=list)
    h: list[float] = Field(default_factory=list)
    low: list[float] = Field(default_factory=list, alias="l")
    c: list[float] = Field(default_factory=list)
    v: list[float] = Field(default_factory=list)
    next_time: int | None = Field(default=None, alias="nextTime")
    errmsg: str | None = None

    model_config = {"populate_by_name": True}


class DatafeedConfig(BaseModel):
    """TradingView `/config`. Says plainly what ARGUS does not have.

    `supports_marks` and `supports_timescale_marks` are False because
    ARGUS's overlays — consolidation zones, state-transition markers — are
    Module 21's concern and are not served here. Advertising them and
    returning nothing would make the widget request them forever.
    """

    supported_resolutions: list[str]
    supports_search: bool = True
    supports_group_request: bool = False
    supports_marks: bool = False
    supports_timescale_marks: bool = False
    supports_time: bool = True
    exchanges: list[dict[str, str]] = Field(default_factory=list)
    symbols_types: list[dict[str, str]] = Field(default_factory=list)


class WatchlistItem(BaseModel):
    """One security on a watchlist.

    Carries `security_id` *and* `ticker` for the reason stated at the top
    of this file: the list is stored against identity, and the ticker is
    the current display of it.
    """

    security_id: UUID
    ticker: str | None = None
    name: str | None = None
    position: int | None = None
    added_at: datetime


class WatchlistSummary(BaseModel):
    """A watchlist without its contents — what a sidebar renders."""

    id: UUID
    name: str
    item_count: int
    created_at: datetime
    updated_at: datetime


class WatchlistDetail(BaseModel):
    """A watchlist with its contents, in the user's chosen order."""

    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime
    items: list[WatchlistItem] = Field(default_factory=list)


class ScanStatusResponse(BaseModel):
    """Whether ARGUS scanned a given date, and what it saw.

    Freshness metadata, deliberately not intelligence: no per-security
    signals, no rankings, no watchlists. The Terminal needs to say "data
    as of X" honestly, which requires knowing whether a scan happened.

    `available` is the distinction Module 18's report made binding: false
    means nobody scanned this date, true with zero counts means a scan ran
    and found nothing — which is currently the normal, correct outcome.
    Flattening the two into "no data" would report a working system as a
    broken one.
    """

    scan_date: date
    available: bool
    status: str | None = None
    scored_signals: int = 0
    setups_opened: int = 0
    excluded_count: int = 0
    explanation: str
