"""ARGUS canonical record types.

**These are the only shapes any module after this one may import.** No
module downstream of normalization references an FMP field name, an FMP
response envelope, or `data.provider_adapters` at all. That is what makes
adding a second provider a matter of writing one adapter plus one
translator, rather than touching the feature engine, the state machine,
and everything else.

Every record carries `pit` (the four timestamps) and `lineage` (which
provider and endpoint it came from), because a canonical row must be
explainable back to its source without consulting anything else.

Securities are identified by `security_id` — the stable internal
identity — never by ticker. Tickers get reassigned and recycled; identity
does not.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from data.canonical_model.exchanges import CanonicalExchange
from data.canonical_model.pit import PitTimestamps


class CanonicalTimeframe(StrEnum):
    """Bar timeframes. Mirrors Module 03's `timeframe` enum.

    Only DAILY is sourced from a provider; H4/WEEKLY/MONTHLY are derived
    from daily by Module 08, so ARGUS keeps point-in-time control over
    them rather than inheriting a vendor's aggregation.
    """

    H4 = "H4"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class CanonicalCorporateActionType(StrEnum):
    """Mirrors Module 03's `corporate_action_type` enum."""

    SPLIT = "SPLIT"
    DIVIDEND = "DIVIDEND"
    MERGER = "MERGER"
    DELISTING = "DELISTING"
    BANKRUPTCY = "BANKRUPTCY"
    TICKER_CHANGE = "TICKER_CHANGE"


class CanonicalStatementType(StrEnum):
    """Canonical fundamentals statement taxonomy.

    Module 03 left `statement_type` as free text precisely so this module
    could define the taxonomy. These are the values it may hold.

    **The line is what a value describes, not who arithmetic-ed it.**
    Everything here is *this issuer's own figures for a closed fiscal
    period*: the three statements as filed, and the metrics, ratios and
    scores a provider derives from them. `KEY_METRICS` and `RATIOS` were
    always provider-computed, and `FINANCIAL_SCORES` joins them on the
    same footing — an Altman Z-Score is arithmetic over a balance sheet,
    reproducible from figures the company filed.

    What does *not* belong here is a claim about the future or about the
    company from someone else: an analyst's forecast, a rating, a price
    target. Those are a different kind of evidence, they carry no fiscal
    period in the same sense, and every reader of `canonical_fundamentals`
    — `bankruptcy.py`'s distress signals among them — is entitled to
    assume a row here is grounded in a filing. See
    `CanonicalDisclosureType` for where the rest goes.
    """

    INCOME_STATEMENT = "INCOME_STATEMENT"
    BALANCE_SHEET = "BALANCE_SHEET"
    CASH_FLOW = "CASH_FLOW"
    KEY_METRICS = "KEY_METRICS"
    RATIOS = "RATIOS"
    #: Altman Z-Score, Piotroski F-Score and the provider's own
    #: solvency/strength figures. A dedicated FMP endpoint rather than a
    #: subset of the two above, which is why it needs its own member.
    FINANCIAL_SCORES = "FINANCIAL_SCORES"


class CanonicalDisclosureType(StrEnum):
    """Period-keyed records that are not financial statements.

    Each of these has a fiscal period and a payload, exactly like a
    statement — and each is a different *kind of claim* from one, which
    is why they live in `canonical_disclosures` rather than beside the
    filings:

    - `ANALYST_ESTIMATES` is what analysts predict, not what happened.
    - `EXECUTIVE_COMPENSATION` is a governance disclosure from the proxy
      statement, not a line on any of the three statements.
    - `EARNINGS_TRANSCRIPT` is what was said on a call — prose, not
      figures, and never parsed into any by ARGUS.

    Restatement works the same way it does for fundamentals: a revision
    is a new row with a later `observation_time`, and a point-in-time
    query picks the latest one that was available at the instant asked
    about. Estimates in particular are revised constantly, which is
    precisely why they need that machinery rather than an overwrite.
    """

    ANALYST_ESTIMATES = "ANALYST_ESTIMATES"
    EXECUTIVE_COMPENSATION = "EXECUTIVE_COMPENSATION"
    EARNINGS_TRANSCRIPT = "EARNINGS_TRANSCRIPT"


class CanonicalSnapshotType(StrEnum):
    """Records describing a security *now*, with no fiscal period at all.

    A price target consensus, a peer group and a fund's holdings are all
    rolling states rather than periods: asking "which quarter is this
    from" has no answer. They are stored as a series of observations —
    one row per fetch that saw something new — so "what did the consensus
    say last March" stays answerable, which a single overwritten current
    row could not do.
    """

    #: The two halves of the price-target picture, kept as separate types
    #: rather than one. Not a taxonomy preference — the snapshot key is
    #: (security, type, observation_time), so filing both under one type
    #: makes two fetches that resolve to the same observation instant
    #: collide, and `ON CONFLICT DO NOTHING` would silently drop whichever
    #: arrived second. Which half survived would then depend on request
    #: ordering, which is exactly the kind of quiet wrongness the
    #: append-only design exists to prevent.
    PRICE_TARGET_CONSENSUS = "PRICE_TARGET_CONSENSUS"
    PRICE_TARGET_SUMMARY = "PRICE_TARGET_SUMMARY"
    PEERS = "PEERS"
    FUND_HOLDINGS = "FUND_HOLDINGS"


class SourceLineage(BaseModel):
    """Which provider and endpoint a canonical record came from.

    Provider-agnostic by design: the *values* name FMP today, but no
    downstream module branches on them. Lineage is for auditing a row
    back to its origin, not for dispatch.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    endpoint: str
    #: True when the source response was served from the adapter's cache.
    #: The PIT ingestion_time is still the ORIGINAL fetch time.
    from_cache: bool = False


class CanonicalRecord(BaseModel):
    """Base for every canonical record."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    security_id: UUID
    pit: PitTimestamps
    lineage: SourceLineage


class CanonicalSecurity(BaseModel):
    """A security's stable identity and its ticker at a point in time.

    Not a `CanonicalRecord`: identity is not an observation with its own
    PIT timestamps, it is the anchor everything else references.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    security_id: UUID
    symbol: str
    exchange: CanonicalExchange
    name: str | None = None
    #: Validity window of this ticker for this security. `valid_to` of
    #: None means "still current".
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class CanonicalOhlcvBar(CanonicalRecord):
    """One bar, carrying both the raw and the adjusted series.

    Both are retained on purpose. Raw is what actually printed that day —
    the point-in-time reality a trader would have seen, and the only
    honest basis for "would this have worked". Adjusted is the
    split/dividend-continuous series that makes multi-year structure
    comparable. Collapsing them loses one irrecoverably.

    Adjusted values are nullable because adjustment depends on corporate
    actions that may not have been fetched yet; a bar is still valid
    without them.
    """

    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY

    open_raw: Decimal
    high_raw: Decimal
    low_raw: Decimal
    close_raw: Decimal
    volume_raw: int

    open_adjusted: Decimal | None = None
    high_adjusted: Decimal | None = None
    low_adjusted: Decimal | None = None
    close_adjusted: Decimal | None = None
    volume_adjusted: int | None = None


class CanonicalFundamental(CanonicalRecord):
    """One financial statement for one fiscal period.

    `pit.event_time` is the fiscal period end — when the period actually
    closed. `pit.observation_time` is the filing's accepted timestamp —
    when the numbers first existed publicly. Those differ by weeks, and
    conflating them is the leakage this module exists to prevent.

    Line items stay in `data` rather than becoming typed fields: the set
    varies by statement type and by what a provider reports, and Module
    03 stores them as JSONB for the same reason.
    """

    statement_type: CanonicalStatementType
    fiscal_period: str
    fiscal_period_end: date
    reported_currency: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class CanonicalCorporateAction(CanonicalRecord):
    """A split, dividend, or other corporate event.

    The event as reported. Applying it to a price series is
    `data.normalization.adjustments`, which reads these rows — the record
    itself is never pre-adjusted.
    """

    action_type: CanonicalCorporateActionType
    effective_date: date
    details: dict[str, Any] = Field(default_factory=dict)


class CanonicalNewsArticle(CanonicalRecord):
    """A news item, for the Terminal page (Module 19). Never scored.

    NOTE: Module 03 defines no news table, so this type has no
    persistence path in this module. It is defined because Module 05's
    scope names it and Module 19 will need it; the missing table is
    flagged in the module README rather than invented here.
    """

    headline: str
    published_at: datetime
    source_site: str | None = None
    url: str | None = None
    summary: str | None = None
