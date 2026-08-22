"""Enumerated domains used by the ARGUS schema.

These are declared once here and reused by the table definitions so the
Python-side names and the PostgreSQL enum types can never drift apart.

Note on `pending_material_events.event_type`: it is deliberately NOT an
enum. Module 12 must be able to add new material-event kinds (litigation,
M&A, trial results, patent decisions) without a schema migration, so that
column is free text. Everything here is a closed domain where adding a
value is a deliberate, reviewable schema change.
"""

from enum import StrEnum


class Timeframe(StrEnum):
    """Bar timeframe for canonical OHLCV.

    Only DAILY is sourced initially (Module 04); the others exist so the
    multi-timeframe feature work in Module 08 does not require a schema
    change.
    """

    H4 = "H4"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class ListingStatus(StrEnum):
    """Listing status of a security within a universe version.

    DELISTED and BANKRUPT securities are deliberately retained: the
    historical universe must include them or backtests inherit
    survivorship bias. Module 09's live eligibility gate excludes them
    from the *candidate* pool separately — that is a different decision
    from whether they exist in the universe at all.
    """

    LISTED = "LISTED"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"
    BANKRUPT = "BANKRUPT"


class CorporateActionType(StrEnum):
    """Kinds of corporate action tracked in canonical_corporate_actions."""

    SPLIT = "SPLIT"
    DIVIDEND = "DIVIDEND"
    MERGER = "MERGER"
    DELISTING = "DELISTING"
    BANKRUPTCY = "BANKRUPTCY"
    TICKER_CHANGE = "TICKER_CHANGE"


class MarketState(StrEnum):
    """The Market State Engine's nine states (Module 10).

    The nominal forward cycle is
    DOWN_TREND -> BASE_FORMING -> CONSOLIDATION -> ACCUMULATION ->
    BREAKOUT_WATCH -> BREAKOUT_READY -> UPTREND -> DISTRIBUTION ->
    DOWN_TREND, but transitions run backward too (a failed breakout
    returns to CONSOLIDATION). Backward transitions are expected, and the
    transition history is itself a feature — see
    market_state_transitions.

    UNCLASSIFIED sits outside that cycle and is deliberately first: it is
    the honest initial state for a newly listed security, or one with
    insufficient history to classify. Forcing such a security into
    DOWN_TREND would silently corrupt both the derived watchlists and any
    statistic computed over the distribution of states.
    """

    UNCLASSIFIED = "UNCLASSIFIED"
    DOWN_TREND = "DOWN_TREND"
    BASE_FORMING = "BASE_FORMING"
    CONSOLIDATION = "CONSOLIDATION"
    ACCUMULATION = "ACCUMULATION"
    BREAKOUT_WATCH = "BREAKOUT_WATCH"
    BREAKOUT_READY = "BREAKOUT_READY"
    UPTREND = "UPTREND"
    DISTRIBUTION = "DISTRIBUTION"


class EligibilityGate(StrEnum):
    """The hard gates Module 09 applies before anything reaches scoring."""

    DATA_HISTORY = "DATA_HISTORY"
    DATA_QUALITY = "DATA_QUALITY"
    LIQUIDITY = "LIQUIDITY"
    BANKRUPTCY_RISK = "BANKRUPTCY_RISK"
    MINIMUM_HISTORICAL_ANALOGUES = "MINIMUM_HISTORICAL_ANALOGUES"
    VALID_ASSET_IDENTITY = "VALID_ASSET_IDENTITY"


class EvidenceStatus(StrEnum):
    """Whether a signal carries scores at all.

    This is what keeps "we deliberately did not score this" distinct from
    "scored low". A signal row with INSUFFICIENT_EVIDENCE has all five
    numbers NULL, enforced by a CHECK constraint; "no data" is the absence
    of a signal row entirely. Never force a numeric score onto
    insufficient evidence.
    """

    SCORED = "SCORED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class SetupLifecycleStatus(StrEnum):
    """Lifecycle stage a setup_event moves a setup into (Module 14).

    A setup's current status is DERIVED from its event history, never
    stored as a mutable column — see setups / setup_events.
    """

    DETECTION = "DETECTION"
    QUALIFICATION = "QUALIFICATION"
    ACTIVE = "ACTIVE"
    OUTCOME = "OUTCOME"


class OutcomeStatus(StrEnum):
    """Terminal outcome of a setup. Never forced into binary win/loss."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    NO_VALID_OUTCOME = "NO_VALID_OUTCOME"


class ReviewConfidence(StrEnum):
    """Reviewer's confidence in an outcome classification (Module 15)."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class FalsePositiveType(StrEnum):
    """Why a setup turned out to be a false positive (Module 15).

    Nullable on the case record — a successful setup has no false
    positive type.
    """

    A_NO_PATTERN = "A"
    B_PATTERN_NO_EXPANSION = "B"
    C_FALSE_BREAKOUT = "C"
    D_BREAKDOWN = "D"
    E_CATALYST_DRIVEN = "E"
    F_ILLIQUID_DISTORTION = "F"
    G_CORPORATE_ACTION_DISTORTION = "G"


class AnalogueScope(StrEnum):
    """Whether a similarity result covers other assets or the same asset.

    Cross-asset and same-asset analogues are separate evidence, never
    interchangeable and never silently blended — Module 11 reports them
    independently, so they are stored as separate rows.
    """

    CROSS_ASSET = "CROSS_ASSET"
    SAME_ASSET = "SAME_ASSET"


class ValidationRunStatus(StrEnum):
    """Execution status of a model validation run (Module 17)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class HistoricalScanStatus(StrEnum):
    """Review gate on a historical scan's results (Module 17).

    Module 20 (Public Stats API) may only serve runs whose current status
    is APPROVED. This is what stops an unvalidated model's numbers
    reaching the public page.
    """

    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
