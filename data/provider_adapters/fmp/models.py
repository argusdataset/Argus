"""Typed intermediate objects mirroring FMP's response shapes.

These are **not** ARGUS canonical objects. They mirror what FMP returns,
with light cleanup (consistent field names, parsed dates, numbers as
Decimal). Translating them into the canonical schema is Module 05's job,
and keeping that boundary sharp is what makes adding a second provider a
matter of writing one new adapter instead of touching the feature engine.

That is also why unrecognised fields are preserved in `raw` rather than
dropped: this adapter should not be the place where information is
silently lost before Module 05 has decided what matters.

Every record carries `provenance` — which provider, which endpoint, and
when it was fetched. Module 05 needs the fetch timestamp for the
`ingestion_time` PIT field, and the endpoint for lineage.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROVIDER_NAME = "fmp"


class FetchProvenance(BaseModel):
    """Where a record came from and when.

    Frozen and shared by reference across every record in one response,
    so attaching it per record costs a pointer rather than a copy.
    """

    model_config = ConfigDict(frozen=True)

    provider: str = PROVIDER_NAME
    endpoint: str
    url_path: str
    #: When the HTTP response was received. Module 05 maps this to
    #: canonical `ingestion_time`. On a cache hit this is the timestamp of
    #: the ORIGINAL fetch, not of the cache read — the data really was
    #: observed then, and pretending otherwise would misstate the PIT
    #: record.
    fetched_at: datetime
    request_params: dict[str, Any] = Field(default_factory=dict)
    #: True when this response was served from the local cache.
    from_cache: bool = False


class EmptyReason(StrEnum):
    """Why a successful response contained no records.

    An empty result is not an error, but it is also not nothing: which
    kind of empty it is determines whether a gap in the historical record
    is expected or alarming.
    """

    #: HTTP 200 with an empty payload. FMP does not distinguish "no such
    #: symbol" from "no data in this range" here, so neither do we.
    NO_DATA_RETURNED = "no_data_returned"
    #: The symbol was checked against the stock list and is not there.
    SYMBOL_NOT_LISTED = "symbol_not_listed"
    #: The requested window lies entirely outside the symbol's trading life.
    RANGE_OUTSIDE_LISTING = "range_outside_listing"


class FmpRecord(BaseModel):
    """Base for every record this adapter returns."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    provenance: FetchProvenance
    #: Fields FMP returned that this adapter does not model explicitly.
    #: Kept so Module 05 can use them without a change here.
    raw: dict[str, Any] = Field(default_factory=dict)


class SecurityListing(FmpRecord):
    """One row of an exchange listing."""

    symbol: str
    name: str | None = None
    exchange: str | None = None
    exchange_short_name: str | None = None
    security_type: str | None = None


class DelistedSecurity(FmpRecord):
    """A security FMP reports as delisted.

    NOTE: FMP supplies the delisting date and exchange but **not** the
    reason — a bankruptcy and an acquisition look identical here. Module
    09's bankruptcy/going-concern gate cannot be built from this field
    alone; see the module README.
    """

    symbol: str
    company_name: str | None = None
    exchange: str | None = None
    ipo_date: date | None = None
    delisted_date: date | None = None


class DailyBar(FmpRecord):
    """One daily OHLCV bar.

    Both raw and adjusted values are carried where FMP distinguishes
    them. ARGUS needs raw for point-in-time realism (what actually
    printed that day) and adjusted for continuity across splits — the
    canonical schema stores both, so the adapter must not collapse them.
    """

    symbol: str
    bar_date: date
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None
    adjusted_open: Decimal | None = None
    adjusted_high: Decimal | None = None
    adjusted_low: Decimal | None = None
    adjusted_close: Decimal | None = None
    adjusted_volume: int | None = None


class FinancialStatement(FmpRecord):
    """A financial statement or metrics payload for one fiscal period.

    The line items stay in `data` rather than becoming typed fields:
    Module 05 owns the canonical fundamentals taxonomy, and the database
    stores them as JSONB for the same reason.
    """

    symbol: str
    statement_type: str
    fiscal_date: date | None = None
    period: str | None = None
    reported_currency: str | None = None
    #: When the filing was accepted/published, where FMP supplies it.
    #: This is the closest thing FMP gives to an observation time, and it
    #: matters: using the fiscal period end as if the numbers were known
    #: then would leak future information into a backtest.
    accepted_date: datetime | None = None
    filing_date: date | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class CorporateActionKind(StrEnum):
    SPLIT = "split"
    DIVIDEND = "dividend"
    MERGER_ACQUISITION = "merger_acquisition"


class CorporateAction(FmpRecord):
    """A split, dividend, or M&A event as FMP reports it.

    The event only — no adjustment is applied here. Applying corporate
    actions to price series is Module 05's work.
    """

    symbol: str
    kind: CorporateActionKind
    event_date: date | None = None
    #: Splits: numerator/denominator. Dividends: amounts and the various
    #: dividend dates. M&A: counterparty details.
    details: dict[str, Any] = Field(default_factory=dict)


class NewsArticle(FmpRecord):
    """A news item, for the Terminal page (Module 19). Never scored."""

    symbol: str | None = None
    published_at: datetime | None = None
    title: str | None = None
    site: str | None = None
    url: str | None = None
    text: str | None = None


class InstitutionalOwnershipSummary(FmpRecord):
    """One symbol's 13F institutional-ownership summary for one quarter.

    `year`/`quarter` are the request parameters this row answers for, kept
    as typed fields because the adapter controls them regardless of what
    the response echoes back. Everything else — investor counts, share
    totals, the limited call/put figures — stays in `raw`: FMP's exact
    field names for this endpoint are documented but not verified against
    a live key (Ultimate plan, not yet purchased). See
    `core/ownership_signals/translate.py`'s `FIELD_ALIASES`.
    """

    symbol: str
    year: int | None = None
    quarter: int | None = None


class InsiderTransaction(FmpRecord):
    """One insider-trading transaction, as reported to the SEC.

    Deliberately thin: transaction code (buy/sell/award), quantity, price
    and the reporting person's name/position all stay in `raw` and are
    resolved by `core/ownership_signals/translate.py`'s `FIELD_ALIASES` —
    the same "field names not confirmed, several spellings tried" caveat
    as `InstitutionalOwnershipSummary`.
    """

    symbol: str


class SecFiling(FmpRecord):
    """One SEC filing (8-K and, in principle, any other form type).

    `form_type` is read here because `fetch_filings_for_symbol` can filter
    by it; the filing date, accepted timestamp, item numbers and link stay
    in `raw` — see `core/news_signals/filings.py`'s `FIELD_ALIASES`.
    """

    symbol: str
    form_type: str | None = None


class AnalystEstimate(FmpRecord):
    """Consensus revenue/EPS forecasts for one fiscal period.

    A forecast, not a filing — which is why it is stored in
    `canonical_disclosures` rather than beside the statements. Every
    figure stays in `raw`; the period label is resolved downstream
    through `FIELD_ALIASES`.
    """

    symbol: str


class PriceTarget(FmpRecord):
    """One half of the price-target picture, named by which half.

    `source` is `"consensus"` or `"summary"` — the two endpoints report
    different things (the target figures, and the counts behind them) and
    merging them at fetch time would lose which number came from where.
    """

    symbol: str
    source: str


class AnalystGrade(FmpRecord):
    """One firm's rating action: who, when, from what, to what."""

    symbol: str


class ExecutiveCompensation(FmpRecord):
    """Proxy-statement compensation for one executive in one fiscal year."""

    symbol: str


class SecurityPeerGroup(FmpRecord):
    """The provider's peer list for one symbol."""

    symbol: str


class EarningsTranscript(FmpRecord):
    """One earnings call's transcript.

    `year`/`quarter` are the request parameters when the fetcher was
    given them — the adapter controls those regardless of what the
    response echoes — and are resolved from the payload otherwise.
    """

    symbol: str
    year: int | None = None
    quarter: int | None = None


class FundHolding(FmpRecord):
    """One position held by an ETF or mutual fund.

    Both endpoints produce this: an ETF's holdings and a mutual fund's
    disclosure are the same kind of fact about the same kind of vehicle,
    and `source` records which endpoint reported it.
    """

    symbol: str
    source: str


class TechnicalIndicatorPoint(FmpRecord):
    """One indicator value for one bar.

    `indicator`, `period_length` and `timeframe` are request parameters
    the adapter controls, so they are typed fields; the value itself and
    the OHLCV the provider echoes back stay in `raw`, because which key
    holds the number differs per indicator (`rsi`, `adx`, `sma`, …).
    """

    symbol: str
    indicator: str
    period_length: int
    timeframe: str


class EarningsEvent(FmpRecord):
    """A scheduled or historical earnings date.

    Feeds `pending_material_events`. A structurally-driven setup and one
    that is really speculative anticipation of a binary event look
    similar but carry very different risk, which is why the schedule is
    tracked at all.
    """

    symbol: str
    earnings_date: date
    eps_actual: Decimal | None = None
    eps_estimated: Decimal | None = None
    revenue_actual: Decimal | None = None
    revenue_estimated: Decimal | None = None
    #: FMP's marker for whether the release is before/after market hours.
    timing: str | None = None
