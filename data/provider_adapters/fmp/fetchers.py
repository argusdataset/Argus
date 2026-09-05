"""High-level FMP fetch operations.

Each method returns a `FetchResult` carrying typed records plus the
provenance Module 05 needs. Nothing here writes to the database, applies
a corporate action, or produces a canonical object — those are all
Module 05's.

On the fetch strategy for daily prices, which is the one design decision
here worth stating plainly: FMP offers both a per-symbol full-history
endpoint and a bulk endpoint returning every symbol for a single date.
The naming suggests bulk is always better. It is not, because the two
draw on different rate-limit budgets:

- Cold backfill (~30 years, whole universe): per-symbol needs one request
  per symbol (~10k requests at the standard limit — minutes to a couple
  of hours). The bulk endpoint would need one request per *trading day*
  (~7,500) against a limit of roughly one per ten seconds — around 21
  hours. Per-symbol wins by an order of magnitude.
- Daily incremental: bulk needs a single request for the entire
  universe; per-symbol needs ~10,000. Bulk wins overwhelmingly.

So `backfill_daily_history` uses per-symbol with bounded concurrency and
a checkpoint, and `fetch_eod_for_date` uses bulk. Both are provided
because both are right, for different jobs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Generic, TypeVar

from data.provider_adapters.fmp import endpoints
from data.provider_adapters.fmp.checkpoint import JobCheckpoint
from data.provider_adapters.fmp.client import FmpClient
from data.provider_adapters.fmp.errors import FmpError
from data.provider_adapters.fmp.models import (
    AnalystEstimate,
    AnalystGrade,
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    DelistedSecurity,
    EarningsEvent,
    EarningsTranscript,
    EmptyReason,
    ExecutiveCompensation,
    FetchProvenance,
    FinancialStatement,
    FundHolding,
    InsiderTransaction,
    InstitutionalOwnershipSummary,
    NewsArticle,
    PriceTarget,
    SecFiling,
    SecurityListing,
    SecurityPeerGroup,
    TechnicalIndicatorPoint,
)
from packages.config import AppConfig, get_config

RecordT = TypeVar("RecordT")

#: Statement endpoints, keyed by the statement_type recorded on each row.
STATEMENT_ENDPOINTS = {
    "INCOME_STATEMENT": endpoints.INCOME_STATEMENT,
    "BALANCE_SHEET": endpoints.BALANCE_SHEET_STATEMENT,
    "CASH_FLOW": endpoints.CASH_FLOW_STATEMENT,
    "KEY_METRICS": endpoints.KEY_METRICS,
    "RATIOS": endpoints.FINANCIAL_RATIOS,
    # Joins the statement path rather than getting a fetcher of its own:
    # `IngestionConfig.statement_types` is derived from this table, so a
    # type added here is picked up by the tiered deep refresh, stored by
    # `translate_fundamental`, and read by `get_latest_fundamental_as_of`
    # with no further wiring anywhere.
    "FINANCIAL_SCORES": endpoints.FINANCIAL_SCORES,
}


@dataclass(frozen=True, slots=True)
class FetchResult(Generic[RecordT]):
    """Records from one fetch, plus why the set is empty if it is.

    `empty_reason` is populated only when the provider genuinely returned
    nothing. An error never arrives here — it is raised.
    """

    records: list[RecordT]
    provenance: FetchProvenance
    empty_reason: EmptyReason | None = None

    def __bool__(self) -> bool:
        return bool(self.records)

    def __len__(self) -> int:
        return len(self.records)


@dataclass(slots=True)
class BackfillReport:
    """Outcome of a resumable multi-symbol backfill."""

    requested: int = 0
    already_complete: int = 0
    succeeded: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    empty: list[str] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return self.succeeded + len(self.failed)


# --------------------------------------------------------------------------
# Light parsing helpers. Deliberately forgiving: a single malformed field
# in one row must not abort a 10,000-symbol backfill, and Module 05
# validates properly anyway.
# --------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        parsed = _date(value)
        return datetime(parsed.year, parsed.month, parsed.day) if parsed else None


def _rows(body: Any) -> list[dict[str, Any]]:
    """Normalise FMP's several response envelopes to a list of dicts."""
    if body is None:
        return []
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if isinstance(body, dict):
        for key in ("historical", "data", "results"):
            nested = body.get(key)
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, dict)]
        if body:
            return [body]
    return []


def _extra(row: dict[str, Any], consumed: Iterable[str]) -> dict[str, Any]:
    """Fields this adapter did not model, kept rather than dropped."""
    used = set(consumed)
    return {key: value for key, value in row.items() if key not in used}


class FmpFetcher:
    """The adapter's public fetch surface."""

    def __init__(self, client: FmpClient, config: AppConfig | None = None) -> None:
        self._client = client
        self._config = config or get_config()

    # -- Listings -----------------------------------------------------------

    async def fetch_stock_list(self) -> FetchResult[SecurityListing]:
        """Every symbol FMP lists. No hardcoded tickers, no fixed count."""
        body, provenance = await self._client.get(endpoints.STOCK_LIST)
        records = [self._listing(row, provenance) for row in _rows(body)]
        return self._result(records, provenance)

    async def fetch_exchange_listings(
        self, exchanges: Sequence[str] = ("NYSE", "NASDAQ")
    ) -> FetchResult[SecurityListing]:
        """Listings filtered to the given exchanges.

        Filtered client-side from the full stock list rather than by
        per-exchange request: it is one request instead of several, and
        FMP's exchange labelling varies between fields, so matching both
        `exchange` and `exchangeShortName` here is more reliable.
        """
        wanted = {value.upper() for value in exchanges}
        listings = await self.fetch_stock_list()
        matched = [
            record
            for record in listings.records
            if (record.exchange_short_name or "").upper() in wanted
            or (record.exchange or "").upper() in wanted
        ]
        return self._result(matched, listings.provenance)

    async def fetch_delisted_companies(
        self, *, max_pages: int = 100, page_size: int = 100
    ) -> FetchResult[DelistedSecurity]:
        """Delisted securities, paged until exhausted.

        Survivorship-bias resistance depends on this: a backtest run over
        only today's survivors reports results the strategy never could
        have achieved. Note FMP gives the delisting date but not the
        *reason* — see the module README.
        """
        collected: list[DelistedSecurity] = []
        provenance: FetchProvenance | None = None

        for page in range(max_pages):
            body, page_provenance = await self._client.get(
                endpoints.DELISTED_COMPANIES,
                params={"page": page, "limit": page_size},
            )
            provenance = provenance or page_provenance
            rows = _rows(body)
            if not rows:
                break
            collected.extend(self._delisted(row, page_provenance) for row in rows)
            if len(rows) < page_size:
                break

        assert provenance is not None
        return self._result(collected, provenance)

    # -- Prices -------------------------------------------------------------

    async def fetch_daily_history(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> FetchResult[DailyBar]:
        """Full daily history for one symbol. The backfill workhorse."""
        params: dict[str, Any] = {"symbol": symbol}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()

        body, provenance = await self._client.get(
            endpoints.HISTORICAL_PRICE_EOD_FULL, params=params
        )
        records = [self._bar(row, symbol, provenance) for row in _rows(body)]
        return self._result(records, provenance)

    async def fetch_eod_for_date(self, as_of: date) -> FetchResult[DailyBar]:
        """Every symbol's bar for one date. The daily-incremental path."""
        body, provenance = await self._client.get(
            endpoints.EOD_BULK, params={"date": as_of.isoformat()}
        )
        records = [
            self._bar(row, str(row.get("symbol") or ""), provenance)
            for row in _rows(body)
            if row.get("symbol")
        ]
        return self._result(records, provenance)

    async def backfill_daily_history(
        self,
        symbols: Sequence[str],
        *,
        job_name: str = "daily_history_backfill",
        start: date | None = None,
        end: date | None = None,
        on_records=None,
    ) -> BackfillReport:
        """Fetch full history for many symbols, resumably.

        Symbols already completed on a previous run are skipped. Each
        symbol is checkpointed as it finishes, so an interrupted job
        resumes rather than restarting.

        A per-symbol failure is recorded and the job continues: one bad
        ticker must not abort a run of thousands. Failures are left
        *un*-checkpointed as successful, so the next run retries them —
        a transient error must not become a permanent hole in the
        historical record.

        `on_records` is called with each symbol's FetchResult as it
        arrives, so a caller can persist incrementally instead of holding
        the whole universe in memory.
        """
        checkpoint = JobCheckpoint(self._config.providers.fmp_checkpoint_dir, job_name)
        checkpoint.load()

        report = BackfillReport(requested=len(symbols))
        pending = checkpoint.pending(list(symbols))
        report.already_complete = report.requested - len(pending)

        # Throughput is governed by the rate limiter; this just bounds how
        # many coroutines are alive at once.
        semaphore = asyncio.Semaphore(self._config.providers.fmp_max_concurrency)
        lock = asyncio.Lock()

        async def run_one(symbol: str) -> None:
            async with semaphore:
                try:
                    result = await self.fetch_daily_history(symbol, start=start, end=end)
                except FmpError as exc:
                    async with lock:
                        report.failed[symbol] = f"{type(exc).__name__}: {exc}"
                    checkpoint.record(symbol, succeeded=False, error=type(exc).__name__)
                    return

            if on_records is not None:
                outcome = on_records(result)
                if asyncio.iscoroutine(outcome):
                    await outcome

            async with lock:
                report.succeeded += 1
                if not result.records:
                    report.empty.append(symbol)
            checkpoint.record(symbol, succeeded=True, bars=len(result.records))

        await asyncio.gather(*(run_one(symbol) for symbol in pending))
        return report

    # -- Fundamentals -------------------------------------------------------

    async def fetch_financial_statement(
        self,
        symbol: str,
        statement_type: str,
        *,
        period: str = "annual",
        limit: int = 200,
    ) -> FetchResult[FinancialStatement]:
        """One statement type for one symbol.

        `statement_type` must be a key of STATEMENT_ENDPOINTS.
        """
        try:
            endpoint = STATEMENT_ENDPOINTS[statement_type]
        except KeyError:
            raise ValueError(
                f"Unknown statement_type {statement_type!r}; "
                f"expected one of {sorted(STATEMENT_ENDPOINTS)}"
            ) from None

        body, provenance = await self._client.get(
            endpoint, params={"symbol": symbol, "period": period, "limit": limit}
        )
        records = [self._statement(row, symbol, statement_type, provenance) for row in _rows(body)]
        return self._result(records, provenance)

    # -- Corporate actions --------------------------------------------------

    async def fetch_splits(self, symbol: str) -> FetchResult[CorporateAction]:
        body, provenance = await self._client.get(endpoints.SPLITS, params={"symbol": symbol})
        records = [
            self._corporate_action(row, symbol, CorporateActionKind.SPLIT, provenance)
            for row in _rows(body)
        ]
        return self._result(records, provenance)

    async def fetch_dividends(self, symbol: str) -> FetchResult[CorporateAction]:
        body, provenance = await self._client.get(endpoints.DIVIDENDS, params={"symbol": symbol})
        records = [
            self._corporate_action(row, symbol, CorporateActionKind.DIVIDEND, provenance)
            for row in _rows(body)
        ]
        return self._result(records, provenance)

    async def fetch_mergers_acquisitions(
        self, *, page: int = 0, limit: int = 100
    ) -> FetchResult[CorporateAction]:
        body, provenance = await self._client.get(
            endpoints.MERGERS_ACQUISITIONS, params={"page": page, "limit": limit}
        )
        records = [
            self._corporate_action(
                row,
                str(row.get("symbol") or row.get("targetedSymbol") or ""),
                CorporateActionKind.MERGER_ACQUISITION,
                provenance,
            )
            for row in _rows(body)
        ]
        return self._result(records, provenance)

    # -- Calendars and news -------------------------------------------------

    async def fetch_earnings_calendar(self, start: date, end: date) -> FetchResult[EarningsEvent]:
        """Scheduled earnings between two dates. Feeds pending_material_events."""
        body, provenance = await self._client.get(
            endpoints.EARNINGS_CALENDAR,
            params={"from": start.isoformat(), "to": end.isoformat()},
        )
        records = [self._earnings(row, provenance) for row in _rows(body) if row.get("symbol")]
        return self._result(records, provenance)

    async def fetch_news(
        self, symbols: Sequence[str], *, page: int = 0, limit: int = 50
    ) -> FetchResult[NewsArticle]:
        """News for the Terminal page (Module 19). Never feeds scoring."""
        body, provenance = await self._client.get(
            endpoints.STOCK_NEWS,
            params={"symbols": ",".join(symbols), "page": page, "limit": limit},
        )
        records = [self._news(row, provenance) for row in _rows(body)]
        return self._result(records, provenance)

    # -- Ultimate-plan: ownership, insider activity, material events --------

    async def fetch_institutional_ownership(
        self, symbol: str, *, year: int | None = None, quarter: int | None = None
    ) -> FetchResult[InstitutionalOwnershipSummary]:
        """One symbol's 13F summary. `year`/`quarter` default to FMP's own
        "most recent" when omitted."""
        params: dict[str, Any] = {"symbol": symbol}
        if year is not None:
            params["year"] = year
        if quarter is not None:
            params["quarter"] = quarter

        body, provenance = await self._client.get(
            endpoints.INSTITUTIONAL_OWNERSHIP_SUMMARY, params=params
        )
        records = [
            self._institutional_ownership(row, symbol, year, quarter, provenance)
            for row in _rows(body)
        ]
        return self._result(records, provenance)

    async def fetch_insider_trades(
        self, symbol: str, *, page: int = 0, limit: int = 100
    ) -> FetchResult[InsiderTransaction]:
        """One symbol's insider transactions, one page."""
        body, provenance = await self._client.get(
            endpoints.INSIDER_TRADING_SEARCH,
            params={"symbol": symbol, "page": page, "limit": limit},
        )
        records = [self._insider_transaction(row, symbol, provenance) for row in _rows(body)]
        return self._result(records, provenance)

    async def fetch_latest_8k_filings(
        self, *, page: int = 0, limit: int = 100
    ) -> FetchResult[SecFiling]:
        """Every symbol's most recent 8-K filings, one page.

        The bulk path — one request covers the whole market rather than
        one per symbol, the same reason `fetch_eod_for_date` exists
        alongside `fetch_daily_history`. The right tool for a daily
        "who filed today" universe check.
        """
        body, provenance = await self._client.get(
            endpoints.SEC_8K_LATEST, params={"page": page, "limit": limit}
        )
        records = [
            self._sec_filing(row, str(row.get("symbol") or ""), "8-K", provenance)
            for row in _rows(body)
            if row.get("symbol")
        ]
        return self._result(records, provenance)

    async def fetch_filings_for_symbol(
        self, symbol: str, *, form_type: str = "8-K", page: int = 0, limit: int = 100
    ) -> FetchResult[SecFiling]:
        """One symbol's SEC filing history, filtered to `form_type`.

        The per-symbol path — right for a single security's history or a
        backfill, wrong for a daily whole-universe check (see
        `fetch_latest_8k_filings`).
        """
        body, provenance = await self._client.get(
            endpoints.SEC_FILINGS_SEARCH_BY_SYMBOL,
            params={"symbol": symbol, "type": form_type, "page": page, "limit": limit},
        )
        records = [self._sec_filing(row, symbol, form_type, provenance) for row in _rows(body)]
        return self._result(records, provenance)

    # -- Ultimate-plan: analyst, governance and holdings --------------------
    #
    # Eight data types Module 19's Terminal serves. Each returns whole
    # provider rows in `raw`: the field names are documented but not
    # verified against a live key, so nothing is extracted here — see
    # `data/normalization/terminal_records.py`'s FIELD_ALIASES.

    async def fetch_analyst_estimates(
        self, symbol: str, *, period: str = "annual", limit: int = 30
    ) -> FetchResult[AnalystEstimate]:
        """Consensus revenue/EPS forecasts, newest period first."""
        body, provenance = await self._client.get(
            endpoints.ANALYST_ESTIMATES,
            params={"symbol": symbol, "period": period, "limit": limit},
        )
        records = [
            AnalystEstimate(provenance=provenance, symbol=symbol, raw=dict(row))
            for row in _rows(body)
        ]
        return self._result(records, provenance)

    async def fetch_price_target_consensus(self, symbol: str) -> FetchResult[PriceTarget]:
        """High/low/median/consensus target figures."""
        return await self._price_target(symbol, endpoints.PRICE_TARGET_CONSENSUS, "consensus")

    async def fetch_price_target_summary(self, symbol: str) -> FetchResult[PriceTarget]:
        """How many analysts published a target, over several windows."""
        return await self._price_target(symbol, endpoints.PRICE_TARGET_SUMMARY, "summary")

    async def _price_target(
        self, symbol: str, endpoint: endpoints.Endpoint, source: str
    ) -> FetchResult[PriceTarget]:
        body, provenance = await self._client.get(endpoint, params={"symbol": symbol})
        records = [
            PriceTarget(provenance=provenance, symbol=symbol, source=source, raw=dict(row))
            for row in _rows(body)
        ]
        return self._result(records, provenance)

    async def fetch_analyst_grades(
        self, symbol: str, *, limit: int = 100
    ) -> FetchResult[AnalystGrade]:
        """Rating changes, newest first. One record per firm per action."""
        body, provenance = await self._client.get(
            endpoints.ANALYST_GRADES, params={"symbol": symbol, "limit": limit}
        )
        records = [
            AnalystGrade(provenance=provenance, symbol=symbol, raw=dict(row)) for row in _rows(body)
        ]
        return self._result(records, provenance)

    async def fetch_executive_compensation(self, symbol: str) -> FetchResult[ExecutiveCompensation]:
        """Proxy-statement compensation, one record per executive per year."""
        body, provenance = await self._client.get(
            endpoints.EXECUTIVE_COMPENSATION, params={"symbol": symbol}
        )
        records = [
            ExecutiveCompensation(provenance=provenance, symbol=symbol, raw=dict(row))
            for row in _rows(body)
        ]
        return self._result(records, provenance)

    async def fetch_stock_peers(self, symbol: str) -> FetchResult[SecurityPeerGroup]:
        """The provider's peer list for one symbol."""
        body, provenance = await self._client.get(endpoints.STOCK_PEERS, params={"symbol": symbol})
        records = [
            SecurityPeerGroup(provenance=provenance, symbol=symbol, raw=dict(row))
            for row in _rows(body)
        ]
        return self._result(records, provenance)

    async def fetch_earnings_transcript(
        self, symbol: str, *, year: int | None = None, quarter: int | None = None
    ) -> FetchResult[EarningsTranscript]:
        """One call's transcript, or the most recent when no period is named."""
        params: dict[str, Any] = {"symbol": symbol}
        if year is not None:
            params["year"] = year
        if quarter is not None:
            params["quarter"] = quarter

        body, provenance = await self._client.get(endpoints.EARNINGS_TRANSCRIPT, params=params)
        records = [
            EarningsTranscript(
                provenance=provenance, symbol=symbol, year=year, quarter=quarter, raw=dict(row)
            )
            for row in _rows(body)
        ]
        return self._result(records, provenance)

    async def fetch_etf_holdings(self, symbol: str) -> FetchResult[FundHolding]:
        """What an ETF holds, one record per position."""
        return await self._fund_positions(symbol, endpoints.ETF_HOLDINGS, "etf")

    async def fetch_fund_disclosure(self, symbol: str) -> FetchResult[FundHolding]:
        """What a mutual fund disclosed holding, one record per position."""
        return await self._fund_positions(symbol, endpoints.FUND_DISCLOSURE, "mutual_fund")

    async def _fund_positions(
        self, symbol: str, endpoint: endpoints.Endpoint, source: str
    ) -> FetchResult[FundHolding]:
        body, provenance = await self._client.get(endpoint, params={"symbol": symbol})
        records = [
            FundHolding(provenance=provenance, symbol=symbol, source=source, raw=dict(row))
            for row in _rows(body)
        ]
        return self._result(records, provenance)

    async def fetch_technical_indicator(
        self,
        symbol: str,
        indicator: str,
        *,
        period_length: int = 14,
        timeframe: str = "1day",
    ) -> FetchResult[TechnicalIndicatorPoint]:
        """One indicator's series for one symbol.

        `indicator` selects the path segment — see
        `endpoints.TECHNICAL_INDICATORS` for the nine FMP exposes. An
        unknown name is refused here rather than sent, because the
        provider would answer a 404 that looks like "no data" and a
        silently empty series is worse than an error.
        """
        if indicator not in endpoints.TECHNICAL_INDICATORS:
            raise ValueError(
                f"Unknown technical indicator {indicator!r}; "
                f"expected one of {sorted(endpoints.TECHNICAL_INDICATORS)}"
            )

        body, provenance = await self._client.get(
            endpoints.TECHNICAL_INDICATOR,
            params={"symbol": symbol, "periodLength": period_length, "timeframe": timeframe},
            path_params={"indicator": indicator},
        )
        records = [
            TechnicalIndicatorPoint(
                provenance=provenance,
                symbol=symbol,
                indicator=indicator,
                period_length=period_length,
                timeframe=timeframe,
                raw=dict(row),
            )
            for row in _rows(body)
        ]
        return self._result(records, provenance)

    # -- Record construction ------------------------------------------------

    @staticmethod
    def _result(records: list[RecordT], provenance: FetchProvenance) -> FetchResult[RecordT]:
        return FetchResult(
            records=records,
            provenance=provenance,
            empty_reason=None if records else EmptyReason.NO_DATA_RETURNED,
        )

    @staticmethod
    def _listing(row: dict[str, Any], provenance: FetchProvenance) -> SecurityListing:
        consumed = ("symbol", "name", "companyName", "exchange", "exchangeShortName", "type")
        return SecurityListing(
            provenance=provenance,
            symbol=str(row.get("symbol", "")),
            name=row.get("name") or row.get("companyName"),
            exchange=row.get("exchange"),
            exchange_short_name=row.get("exchangeShortName"),
            security_type=row.get("type"),
            raw=_extra(row, consumed),
        )

    @staticmethod
    def _delisted(row: dict[str, Any], provenance: FetchProvenance) -> DelistedSecurity:
        consumed = ("symbol", "companyName", "exchange", "ipoDate", "delistedDate")
        return DelistedSecurity(
            provenance=provenance,
            symbol=str(row.get("symbol", "")),
            company_name=row.get("companyName"),
            exchange=row.get("exchange"),
            ipo_date=_date(row.get("ipoDate")),
            delisted_date=_date(row.get("delistedDate")),
            raw=_extra(row, consumed),
        )

    @staticmethod
    def _bar(row: dict[str, Any], symbol: str, provenance: FetchProvenance) -> DailyBar:
        consumed = (
            "symbol",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "adjOpen",
            "adjHigh",
            "adjLow",
            "adjClose",
            "adjVolume",
            "unadjustedVolume",
        )
        return DailyBar(
            provenance=provenance,
            symbol=symbol,
            bar_date=_date(row.get("date")) or date.min,
            open=_decimal(row.get("open")),
            high=_decimal(row.get("high")),
            low=_decimal(row.get("low")),
            close=_decimal(row.get("close")),
            volume=_int(row.get("volume")),
            adjusted_open=_decimal(row.get("adjOpen")),
            adjusted_high=_decimal(row.get("adjHigh")),
            adjusted_low=_decimal(row.get("adjLow")),
            adjusted_close=_decimal(row.get("adjClose")),
            adjusted_volume=_int(row.get("adjVolume")),
            raw=_extra(row, consumed),
        )

    @staticmethod
    def _statement(
        row: dict[str, Any],
        symbol: str,
        statement_type: str,
        provenance: FetchProvenance,
    ) -> FinancialStatement:
        identifying = (
            "symbol",
            "date",
            "period",
            "reportedCurrency",
            "acceptedDate",
            "filingDate",
        )
        return FinancialStatement(
            provenance=provenance,
            symbol=symbol,
            statement_type=statement_type,
            fiscal_date=_date(row.get("date")),
            period=row.get("period"),
            reported_currency=row.get("reportedCurrency"),
            accepted_date=_datetime(row.get("acceptedDate")),
            filing_date=_date(row.get("filingDate")),
            # Everything that is not an identifier is a line item, left
            # for Module 05 to name canonically.
            data=_extra(row, identifying),
        )

    @staticmethod
    def _corporate_action(
        row: dict[str, Any],
        symbol: str,
        kind: CorporateActionKind,
        provenance: FetchProvenance,
    ) -> CorporateAction:
        return CorporateAction(
            provenance=provenance,
            symbol=symbol,
            kind=kind,
            event_date=_date(row.get("date") or row.get("transactionDate")),
            details=_extra(row, ("symbol", "date")),
        )

    @staticmethod
    def _news(row: dict[str, Any], provenance: FetchProvenance) -> NewsArticle:
        consumed = ("symbol", "publishedDate", "title", "site", "url", "text")
        return NewsArticle(
            provenance=provenance,
            symbol=row.get("symbol"),
            published_at=_datetime(row.get("publishedDate")),
            title=row.get("title"),
            site=row.get("site"),
            url=row.get("url"),
            text=row.get("text"),
            raw=_extra(row, consumed),
        )

    @staticmethod
    def _institutional_ownership(
        row: dict[str, Any],
        symbol: str,
        year: int | None,
        quarter: int | None,
        provenance: FetchProvenance,
    ) -> InstitutionalOwnershipSummary:
        """Nothing consumed but `symbol`: every concept here — investor
        counts, share totals, ownership percent, the limited call/put
        figures — has an unconfirmed field name, so the whole row stays in
        `raw` for `FIELD_ALIASES` to resolve downstream."""
        return InstitutionalOwnershipSummary(
            provenance=provenance,
            symbol=symbol,
            year=year,
            quarter=quarter,
            raw=dict(row),
        )

    @staticmethod
    def _insider_transaction(
        row: dict[str, Any], symbol: str, provenance: FetchProvenance
    ) -> InsiderTransaction:
        """Same reasoning as `_institutional_ownership`: transaction code,
        quantity, price, filer name and position are all unconfirmed field
        names, so nothing is extracted here."""
        return InsiderTransaction(provenance=provenance, symbol=symbol, raw=dict(row))

    @staticmethod
    def _sec_filing(
        row: dict[str, Any], symbol: str, form_type: str, provenance: FetchProvenance
    ) -> SecFiling:
        """`form_type` is carried because both fetch paths already know it
        (a filter param on one, a fixed value on the other); the filing
        date, accepted timestamp, item numbers and link are unconfirmed
        and stay in `raw`."""
        return SecFiling(provenance=provenance, symbol=symbol, form_type=form_type, raw=dict(row))

    @staticmethod
    def _earnings(row: dict[str, Any], provenance: FetchProvenance) -> EarningsEvent:
        consumed = (
            "symbol",
            "date",
            "epsActual",
            "epsEstimated",
            "revenueActual",
            "revenueEstimated",
            "time",
        )
        return EarningsEvent(
            provenance=provenance,
            symbol=str(row.get("symbol", "")),
            earnings_date=_date(row.get("date")) or date.min,
            eps_actual=_decimal(row.get("epsActual")),
            eps_estimated=_decimal(row.get("epsEstimated")),
            revenue_actual=_decimal(row.get("revenueActual")),
            revenue_estimated=_decimal(row.get("revenueEstimated")),
            timing=row.get("time"),
            raw=_extra(row, consumed),
        )
