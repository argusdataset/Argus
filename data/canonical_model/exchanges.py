"""Canonical exchange identity.

Module 04 found FMP labels exchanges inconsistently: the same venue
appears as a long name in `exchange` and an abbreviation in
`exchangeShortName`, and the two do not always agree — SPY comes back
with `exchange="NYSE Arca"` but `exchangeShortName="AMEX"`.

Module 06 builds the universe from "all NYSE + NASDAQ listed securities",
so getting those two right is the part that actually matters. Everything
else resolves to a coarse bucket or to UNKNOWN.

**Unrecognised labels become UNKNOWN, never a guess.** Silently mapping an
unfamiliar venue into NYSE would put securities into the universe that do
not belong there, and no later stage would catch it. UNKNOWN is visible
and can be reported; a wrong guess is not.
"""

from __future__ import annotations

from enum import StrEnum


class CanonicalExchange(StrEnum):
    """The venues ARGUS distinguishes.

    Deliberately coarse. ARGUS's universe is US equities on NYSE and
    NASDAQ; finer venue distinctions have no consumer in any scoped
    module.
    """

    NYSE = "NYSE"
    NASDAQ = "NASDAQ"
    NYSE_AMERICAN = "NYSE_AMERICAN"
    NYSE_ARCA = "NYSE_ARCA"
    OTC = "OTC"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


#: The exchanges Module 06's universe is drawn from.
UNIVERSE_EXCHANGES: frozenset[CanonicalExchange] = frozenset(
    {CanonicalExchange.NYSE, CanonicalExchange.NASDAQ}
)

# Matched against a lowercased, whitespace-collapsed label. Longest match
# wins, so "nyse arca" is not swallowed by "nyse".
_EXCHANGE_PATTERNS: tuple[tuple[str, CanonicalExchange], ...] = (
    ("nasdaq global select", CanonicalExchange.NASDAQ),
    ("nasdaq global market", CanonicalExchange.NASDAQ),
    ("nasdaq capital market", CanonicalExchange.NASDAQ),
    ("nasdaq", CanonicalExchange.NASDAQ),
    ("ndq", CanonicalExchange.NASDAQ),
    ("nyse arca", CanonicalExchange.NYSE_ARCA),
    ("arca", CanonicalExchange.NYSE_ARCA),
    ("nyse american", CanonicalExchange.NYSE_AMERICAN),
    ("amex", CanonicalExchange.NYSE_AMERICAN),
    ("american stock exchange", CanonicalExchange.NYSE_AMERICAN),
    ("new york stock exchange", CanonicalExchange.NYSE),
    ("nyse", CanonicalExchange.NYSE),
    ("otc", CanonicalExchange.OTC),
    ("pink", CanonicalExchange.OTC),
)


def _match(label: str | None) -> CanonicalExchange | None:
    if not label:
        return None
    normalised = " ".join(label.lower().split())
    for pattern, exchange in _EXCHANGE_PATTERNS:
        if pattern in normalised:
            return exchange
    return None


def normalize_exchange(
    exchange: str | None = None,
    exchange_short_name: str | None = None,
) -> CanonicalExchange:
    """Resolve FMP's two exchange labels to one canonical venue.

    The long `exchange` label is consulted first because it is the more
    specific of the two: FMP reports NYSE Arca ETFs with
    `exchangeShortName="AMEX"`, so trusting the short name would file
    them under the wrong venue. Where the long name is absent or
    unrecognised, the short name is used as a fallback.
    """
    return _match(exchange) or _match(exchange_short_name) or CanonicalExchange.UNKNOWN


def is_universe_exchange(exchange: CanonicalExchange) -> bool:
    """Whether Module 06 would draw a security on this venue into the universe."""
    return exchange in UNIVERSE_EXCHANGES


def normalize_symbol(symbol: str) -> str:
    """Canonical form of a ticker string.

    Uppercased and stripped only. Deliberately NOT rewritten further:
    FMP uses suffixes (`.TO`, `.DE`) to denote non-US listings, and
    stripping them would collapse a Toronto listing onto its US
    namesake — exactly the kind of silent wrong join the ticker-history
    exclusion constraints exist to prevent.
    """
    return symbol.strip().upper()
