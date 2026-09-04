"""FMP endpoint paths, defined in exactly one place.

Every endpoint path FMP exposes is declared here rather than inline at
the call site. That is deliberate: FMP has migrated from the legacy
`/api/v3/...` layout to a newer `/stable/...` one, the two overlap
unevenly, and the paths below were assembled from public documentation
rather than verified against a live key (none was available while this
module was built). If a path turns out to be wrong, the correction is a
one-line change here and nothing else in the adapter moves.

`Endpoint.tier` marks which endpoints are bulk CSV downloads, because
those carry a separate and far stricter rate limit than standard
endpoints — see `rate_limit.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EndpointTier(StrEnum):
    """Which rate-limit budget an endpoint draws from."""

    STANDARD = "standard"
    BULK = "bulk"


class ResponseFormat(StrEnum):
    JSON = "json"
    CSV = "csv"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A single FMP endpoint.

    `path` is relative to the configured base URL and may contain
    `{placeholders}` filled in per request.
    """

    name: str
    path: str
    tier: EndpointTier = EndpointTier.STANDARD
    response_format: ResponseFormat = ResponseFormat.JSON
    #: Whether responses are historical facts that never change, and so
    #: may be cached indefinitely. Calendars and news are not.
    immutable: bool = True

    def render(self, **params: str) -> str:
        return self.path.format(**params)


# --- Listings and identity -------------------------------------------------

STOCK_LIST = Endpoint("stock_list", "/stable/stock-list", immutable=False)
COMPANY_SCREENER = Endpoint("company_screener", "/stable/company-screener", immutable=False)
DELISTED_COMPANIES = Endpoint("delisted_companies", "/stable/delisted-companies", immutable=False)
COMPANY_PROFILE = Endpoint("company_profile", "/stable/profile", immutable=False)

# --- Prices ----------------------------------------------------------------

#: Full daily history for ONE symbol. This is the workhorse for the
#: initial backfill — see the README on why per-symbol beats the bulk
#: endpoint for a cold load despite the naming.
HISTORICAL_PRICE_EOD_FULL = Endpoint(
    "historical_price_eod_full", "/stable/historical-price-eod/full"
)

#: All symbols for ONE date. The right tool for the daily incremental
#: update (one request covers the whole universe), and the wrong tool for
#: a cold backfill (it would need one request per trading day, against a
#: much stricter limit).
EOD_BULK = Endpoint(
    "eod_bulk",
    "/stable/eod-bulk",
    tier=EndpointTier.BULK,
    response_format=ResponseFormat.CSV,
)

# --- Fundamentals ----------------------------------------------------------

INCOME_STATEMENT = Endpoint("income_statement", "/stable/income-statement")
BALANCE_SHEET_STATEMENT = Endpoint("balance_sheet_statement", "/stable/balance-sheet-statement")
CASH_FLOW_STATEMENT = Endpoint("cash_flow_statement", "/stable/cash-flow-statement")
KEY_METRICS = Endpoint("key_metrics", "/stable/key-metrics")
FINANCIAL_RATIOS = Endpoint("financial_ratios", "/stable/ratios")

# --- Corporate actions -----------------------------------------------------

SPLITS = Endpoint("splits", "/stable/splits")
DIVIDENDS = Endpoint("dividends", "/stable/dividends")
MERGERS_ACQUISITIONS = Endpoint(
    "mergers_acquisitions", "/stable/mergers-acquisitions-latest", immutable=False
)

# --- Calendars and news ----------------------------------------------------

EARNINGS_CALENDAR = Endpoint("earnings_calendar", "/stable/earnings-calendar", immutable=False)
STOCK_NEWS = Endpoint("stock_news", "/stable/news/stock", immutable=False)

# --- Ultimate-plan: ownership, insider activity, material events -----------
#
# Added for the $149/mo Ultimate tier (3000 req/min). Documented FMP paths,
# not verified against a live key — the same caveat SPLITS/DIVIDENDS-era
# endpoints carried before them. See core/news_signals/filings.py and
# core/ownership_signals/ for the FIELD_ALIASES tolerance this buys.

#: Per-symbol, one quarter's institutional 13F summary. `year`/`quarter`
#: are request params, not part of the path.
INSTITUTIONAL_OWNERSHIP_SUMMARY = Endpoint(
    "institutional_ownership_summary",
    "/stable/institutional-ownership/symbol-positions-summary",
    immutable=False,
)

#: Per-symbol insider transactions, paged.
INSIDER_TRADING_SEARCH = Endpoint(
    "insider_trading_search", "/stable/insider-trading/search", immutable=False
)

#: Every symbol's most recent 8-K filings, paged. The bulk counterpart to
#: SEARCH_BY_SYMBOL below — the right tool for "who filed today", the same
#: role EOD_BULK plays for prices.
SEC_8K_LATEST = Endpoint("sec_8k_latest", "/stable/8k-latest", immutable=False)

#: One symbol's SEC filing history, filterable by form type. The right
#: tool for a single security's history; the wrong one for a daily
#: universe-wide "did anyone file today" check.
SEC_FILINGS_SEARCH_BY_SYMBOL = Endpoint(
    "sec_filings_search_by_symbol", "/stable/search-by-symbol", immutable=False
)

#: Every endpoint above, by name — used by tests to assert the registry
#: and the fetchers stay in step.
ALL_ENDPOINTS: tuple[Endpoint, ...] = (
    STOCK_LIST,
    COMPANY_SCREENER,
    DELISTED_COMPANIES,
    COMPANY_PROFILE,
    HISTORICAL_PRICE_EOD_FULL,
    EOD_BULK,
    INCOME_STATEMENT,
    BALANCE_SHEET_STATEMENT,
    CASH_FLOW_STATEMENT,
    KEY_METRICS,
    FINANCIAL_RATIOS,
    SPLITS,
    DIVIDENDS,
    MERGERS_ACQUISITIONS,
    EARNINGS_CALENDAR,
    STOCK_NEWS,
    INSTITUTIONAL_OWNERSHIP_SUMMARY,
    INSIDER_TRADING_SEARCH,
    SEC_8K_LATEST,
    SEC_FILINGS_SEARCH_BY_SYMBOL,
)
