"""Which securities are admitted to the universe, and what gets reported.

The universe is exactly what the exchanges report — no hardcoded ticker
list, no fixed size, no assumed count anywhere. Admission is therefore
narrow: classify the venue, exclude anything that is not NYSE or NASDAQ,
and **report every exclusion**.

That last part is the load-bearing one. Module 05's instruction, carried
forward verbatim: *report `UNKNOWN` counts, never drop them silently.* If
FMP renames a venue label — "NASDAQ Global Select" becoming something
`normalize_exchange` does not recognise — those securities fall to
`UNKNOWN` and vanish from the universe. Nothing downstream would fail.
The scan would just quietly cover fewer stocks, and the first hint would
be an unexplained drop in signal counts months later.

So exclusions are counted by reason, sampled, and returned with the
construction result. A caller that ignores the report gets a correct
universe; a caller that reads it can see the universe shrink.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from data.canonical_model.exchanges import CanonicalExchange, is_universe_exchange

#: Ticker suffixes that denote a non-US venue. Used as a secondary guard:
#: a security whose exchange label says NYSE/NASDAQ but whose symbol
#: carries one of these is a provider data error, not a US listing.
#:
#: Deliberately an explicit list rather than "any dot suffix". US class
#: shares use dotted tickers too (BRK.B, BF.B), and blanket-excluding
#: every dot would drop real NYSE constituents — a silent universe
#: shrink of exactly the kind this module exists to prevent.
FOREIGN_SYMBOL_SUFFIXES: frozenset[str] = frozenset(
    {
        "AS",
        "AX",
        "BO",
        "BR",
        "CO",
        "DE",
        "F",
        "HE",
        "HK",
        "IR",
        "JK",
        "KS",
        "KQ",
        "L",
        "LS",
        "MC",
        "MI",
        "MX",
        "NS",
        "NZ",
        "OL",
        "PA",
        "SA",
        "SI",
        "SS",
        "ST",
        "SW",
        "SZ",
        "T",
        "TA",
        "TO",
        "TW",
        "V",
        "VI",
        "WA",
    }
)


class ExclusionReason(StrEnum):
    """Why a security did not enter the universe."""

    #: Venue label was not recognised. The one that matters most — see the
    #: module docstring.
    UNKNOWN_EXCHANGE = "unknown_exchange"
    #: Recognised, but not NYSE or NASDAQ (Arca, NYSE American, OTC, ...).
    NON_UNIVERSE_EXCHANGE = "non_universe_exchange"
    #: Symbol carries a non-US venue suffix despite its exchange label.
    FOREIGN_SYMBOL_SUFFIX = "foreign_symbol_suffix"
    #: Ticker could not be resolved to a security identity.
    UNRESOLVED_IDENTITY = "unresolved_identity"
    #: Provider row had no usable symbol.
    MISSING_SYMBOL = "missing_symbol"


@dataclass(frozen=True, slots=True)
class Exclusion:
    """One security kept out of the universe, and why."""

    symbol: str
    reason: ExclusionReason
    exchange_label: str | None = None


@dataclass(slots=True)
class AdmissionReport:
    """What was admitted, what was not, and why.

    Never a bare count: the samples make an unexpected exclusion spike
    diagnosable without re-running the fetch.
    """

    #: Securities admitted, by canonical exchange.
    admitted_by_exchange: Counter[CanonicalExchange] = field(default_factory=Counter)
    excluded_by_reason: Counter[ExclusionReason] = field(default_factory=Counter)
    #: A bounded sample per reason, for diagnosis.
    samples: dict[ExclusionReason, list[Exclusion]] = field(default_factory=dict)
    #: Distinct raw venue labels that did not resolve. This is the list to
    #: read when the universe shrinks unexpectedly.
    unknown_exchange_labels: Counter[str] = field(default_factory=Counter)

    sample_limit: int = 25

    @property
    def admitted(self) -> int:
        return sum(self.admitted_by_exchange.values())

    @property
    def excluded(self) -> int:
        return sum(self.excluded_by_reason.values())

    @property
    def unknown_exchange_count(self) -> int:
        """Securities dropped because their venue label was unrecognised."""
        return self.excluded_by_reason[ExclusionReason.UNKNOWN_EXCHANGE]

    def admit(self, exchange: CanonicalExchange) -> None:
        self.admitted_by_exchange[exchange] += 1

    def exclude(
        self,
        symbol: str,
        reason: ExclusionReason,
        *,
        exchange_label: str | None = None,
    ) -> None:
        self.excluded_by_reason[reason] += 1
        bucket = self.samples.setdefault(reason, [])
        if len(bucket) < self.sample_limit:
            bucket.append(Exclusion(symbol, reason, exchange_label))
        if reason is ExclusionReason.UNKNOWN_EXCHANGE and exchange_label:
            self.unknown_exchange_labels[exchange_label] += 1

    def summary(self) -> dict[str, object]:
        """Serializable summary, suitable for logging or a version record."""
        return {
            "admitted": self.admitted,
            "admitted_by_exchange": {
                exchange.value: count for exchange, count in self.admitted_by_exchange.items()
            },
            "excluded": self.excluded,
            "excluded_by_reason": {
                reason.value: count for reason, count in self.excluded_by_reason.items()
            },
            "unknown_exchange_labels": dict(self.unknown_exchange_labels.most_common(20)),
        }


def symbol_suffix(symbol: str) -> str | None:
    """The venue suffix of a ticker, if it has one."""
    _, separator, suffix = symbol.rpartition(".")
    if not separator or not suffix:
        return None
    return suffix.upper()


def has_foreign_suffix(symbol: str) -> bool:
    """Whether a ticker denotes a non-US venue.

    Module 05 preserved these suffixes precisely so this check is
    possible — stripping them would have collapsed a Toronto listing onto
    its US namesake.
    """
    suffix = symbol_suffix(symbol)
    return suffix is not None and suffix in FOREIGN_SYMBOL_SUFFIXES


def admits(exchange: CanonicalExchange, symbol: str) -> ExclusionReason | None:
    """Whether a security belongs in the universe; the reason if not.

    Returns None when admitted. The exchange classification is the primary
    gate; the symbol suffix is a secondary guard against a provider row
    whose label and ticker disagree.
    """
    if not symbol:
        return ExclusionReason.MISSING_SYMBOL
    if exchange is CanonicalExchange.UNKNOWN:
        return ExclusionReason.UNKNOWN_EXCHANGE
    if not is_universe_exchange(exchange):
        return ExclusionReason.NON_UNIVERSE_EXCHANGE
    if has_foreign_suffix(symbol):
        return ExclusionReason.FOREIGN_SYMBOL_SUFFIX
    return None
