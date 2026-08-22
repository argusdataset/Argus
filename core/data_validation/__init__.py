"""Data Validation — built in Module 07.

The point-in-time enforcement layer. Every future module reads
canonical/derived data through `get_as_of`, never through raw SQL that
could bypass `availability_time` filtering — that is the entire reason
this module exists. See the README for the adversarial leakage test that
proves the enforcement actually holds.

    from core.data_validation import get_as_of, EntityType

    result = get_as_of(connection, EntityType.FUNDAMENTALS, security_id, as_of,
                        statement_type=CanonicalStatementType.INCOME_STATEMENT)
    if result:
        use(result.value)

Also home to gap and duplicate detection — advisory integrity checks on
already-ingested canonical data, distinct from the PIT query layer.
"""

from core.data_validation.calendar import expected_trading_days, is_trading_day, us_market_holidays
from core.data_validation.corporate_actions import CorporateActionAsOf, get_corporate_actions_as_of
from core.data_validation.duplicates import DuplicateBar, detect_duplicate_bars
from core.data_validation.entities import EntityType
from core.data_validation.feature_vectors import FeatureVectorAsOf, get_feature_vector_as_of
from core.data_validation.fundamentals import (
    FundamentalAsOf,
    get_fundamental_as_of,
    get_latest_fundamental_as_of,
)
from core.data_validation.gaps import GapReport, detect_gaps
from core.data_validation.ohlcv import OhlcvBarAsOf, get_ohlcv_bar_as_of
from core.data_validation.query import get_as_of
from core.data_validation.result import AsOfResult, MissReason
from core.data_validation.universe import (
    MembershipAsOf,
    get_universe_membership_as_of,
    list_universe_members_as_of,
    parse_interval_evidence,
)

__all__ = [
    "AsOfResult",
    "CorporateActionAsOf",
    "DuplicateBar",
    "EntityType",
    "FeatureVectorAsOf",
    "FundamentalAsOf",
    "GapReport",
    "MembershipAsOf",
    "MissReason",
    "OhlcvBarAsOf",
    "detect_duplicate_bars",
    "detect_gaps",
    "expected_trading_days",
    "get_as_of",
    "get_corporate_actions_as_of",
    "get_feature_vector_as_of",
    "get_fundamental_as_of",
    "get_latest_fundamental_as_of",
    "get_ohlcv_bar_as_of",
    "get_universe_membership_as_of",
    "is_trading_day",
    "list_universe_members_as_of",
    "parse_interval_evidence",
    "us_market_holidays",
]
