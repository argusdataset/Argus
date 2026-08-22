"""Universe construction and versioning — built in Module 06.

Answers "which securities were listed on NYSE/NASDAQ as of date X" for
any X, not just today. Historical versions include securities that have
since been delisted, which is what keeps backtests free of survivorship
bias.

    from core.universe import build_intervals_from_fetch, construct_version

Consumes Module 05's identity resolution and exchange normalization; it
does not mint identities or re-solve exchange labels.
"""

from core.universe.admission import (
    FOREIGN_SYMBOL_SUFFIXES,
    AdmissionReport,
    Exclusion,
    ExclusionReason,
    admits,
    has_foreign_suffix,
)
from core.universe.builder import (
    UNIVERSE_EXCHANGE_NAMES,
    UniverseConstruction,
    bar_date_bounds,
    build_intervals_from_fetch,
    construct_version,
)
from core.universe.intervals import (
    IntervalEvidence,
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
)

__all__ = [
    "FOREIGN_SYMBOL_SUFFIXES",
    "UNIVERSE_EXCHANGE_NAMES",
    "AdmissionReport",
    "Exclusion",
    "ExclusionReason",
    "IntervalEvidence",
    "ListingInterval",
    "ListingObservation",
    "StoredUniverseVersion",
    "UniverseConstruction",
    "UniverseRepository",
    "admits",
    "bar_date_bounds",
    "build_intervals",
    "build_intervals_from_fetch",
    "construct_version",
    "default_version_label",
    "has_foreign_suffix",
    "intervals_covering",
    "membership_checksum",
]
