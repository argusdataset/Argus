"""ARGUS canonical data model — built in Module 05.

The only data shapes any module after normalization may import. Nothing
downstream references a provider's field names or response envelopes.

    from data.canonical_model import CanonicalOhlcvBar, PitTimestamps
"""

from data.canonical_model.exchanges import (
    UNIVERSE_EXCHANGES,
    CanonicalExchange,
    is_universe_exchange,
    normalize_exchange,
    normalize_symbol,
)
from data.canonical_model.pit import (
    DEFAULT_LAG_POLICY,
    MARKET_CLOSE,
    MARKET_TIMEZONE,
    PitTimestamps,
    ProviderLagPolicy,
    session_close,
)
from data.canonical_model.records import (
    CanonicalCorporateAction,
    CanonicalCorporateActionType,
    CanonicalFundamental,
    CanonicalNewsArticle,
    CanonicalOhlcvBar,
    CanonicalRecord,
    CanonicalSecurity,
    CanonicalStatementType,
    CanonicalTimeframe,
    SourceLineage,
)

__all__ = [
    "DEFAULT_LAG_POLICY",
    "MARKET_CLOSE",
    "MARKET_TIMEZONE",
    "UNIVERSE_EXCHANGES",
    "CanonicalCorporateAction",
    "CanonicalCorporateActionType",
    "CanonicalExchange",
    "CanonicalFundamental",
    "CanonicalNewsArticle",
    "CanonicalOhlcvBar",
    "CanonicalRecord",
    "CanonicalSecurity",
    "CanonicalStatementType",
    "CanonicalTimeframe",
    "PitTimestamps",
    "ProviderLagPolicy",
    "SourceLineage",
    "is_universe_exchange",
    "normalize_exchange",
    "normalize_symbol",
    "session_close",
]
