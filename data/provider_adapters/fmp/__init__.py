"""FMP provider adapter — built in Module 04.

Talks to Financial Modeling Prep and hands back typed intermediate
objects. It deliberately does **not** produce ARGUS canonical objects,
write to the database, or apply corporate actions: that is Module 05's
boundary, and keeping it sharp is what makes adding a second provider a
matter of writing one new adapter rather than touching the feature
engine, the state machine, or anything else downstream.

Usage:

    from data.provider_adapters.fmp import FmpClient, FmpFetcher

    async with FmpClient() as client:
        fetcher = FmpFetcher(client)
        listings = await fetcher.fetch_exchange_listings(("NYSE", "NASDAQ"))
"""

from data.provider_adapters.fmp import endpoints
from data.provider_adapters.fmp.cache import ResponseCache
from data.provider_adapters.fmp.checkpoint import JobCheckpoint
from data.provider_adapters.fmp.client import FMP_API_KEY_SECRET, FmpClient
from data.provider_adapters.fmp.errors import (
    FmpAuthenticationError,
    FmpError,
    FmpProviderError,
    FmpRateLimitError,
    FmpSymbolNotFoundError,
    FmpTransportError,
)
from data.provider_adapters.fmp.fetchers import BackfillReport, FetchResult, FmpFetcher
from data.provider_adapters.fmp.models import (
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    DelistedSecurity,
    EarningsEvent,
    EmptyReason,
    FetchProvenance,
    FinancialStatement,
    NewsArticle,
    SecurityListing,
)
from data.provider_adapters.fmp.rate_limit import RateLimiter, TokenBucket

__all__ = [
    "FMP_API_KEY_SECRET",
    "BackfillReport",
    "CorporateAction",
    "CorporateActionKind",
    "DailyBar",
    "DelistedSecurity",
    "EarningsEvent",
    "EmptyReason",
    "FetchProvenance",
    "FetchResult",
    "FinancialStatement",
    "FmpAuthenticationError",
    "FmpClient",
    "FmpError",
    "FmpFetcher",
    "FmpProviderError",
    "FmpRateLimitError",
    "FmpSymbolNotFoundError",
    "FmpTransportError",
    "JobCheckpoint",
    "NewsArticle",
    "RateLimiter",
    "ResponseCache",
    "SecurityListing",
    "TokenBucket",
    "endpoints",
]
