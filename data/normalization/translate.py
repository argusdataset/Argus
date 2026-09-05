"""Provider records to canonical records.

This is the leakage boundary in code. For each data type it decides
which source field supplies `observation_time` — the moment ARGUS could
first have known the value — and that decision is the difference between
an honest backtest and one that quietly sees the future.

Summary of the sourcing decisions, with reasoning in each function:

| Canonical type   | event_time            | observation_time              |
|------------------|-----------------------|-------------------------------|
| OHLCV bar        | session close         | session close (bar completes) |
| Fundamentals     | fiscal period end     | **acceptedDate** (never period end) |
| Corporate action | effective/ex date     | declaration date, else effective |
| News             | publication time      | publication time              |

`availability_time` is derived as `observation_time + lag` in every case:
FMP does not report when a record entered its dataset. See
`data.canonical_model.pit` for why the lags err late.

`ingestion_time` is always the adapter's `provenance.fetched_at`, which
Module 04 guarantees is the ORIGINAL fetch time even on a cache hit.
Using the cache-read time instead would claim ARGUS observed the data
later than it did, misstating the record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from data.canonical_model.pit import (
    DEFAULT_LAG_POLICY,
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
    CanonicalStatementType,
    CanonicalTimeframe,
    SourceLineage,
)
from data.provider_adapters.fmp.models import (
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    FetchProvenance,
    FinancialStatement,
    NewsArticle,
)

#: FMP statement_type values mapped to the canonical taxonomy.
_STATEMENT_TYPES: dict[str, CanonicalStatementType] = {
    "INCOME_STATEMENT": CanonicalStatementType.INCOME_STATEMENT,
    "BALANCE_SHEET": CanonicalStatementType.BALANCE_SHEET,
    "CASH_FLOW": CanonicalStatementType.CASH_FLOW,
    "KEY_METRICS": CanonicalStatementType.KEY_METRICS,
    "RATIOS": CanonicalStatementType.RATIOS,
    "FINANCIAL_SCORES": CanonicalStatementType.FINANCIAL_SCORES,
}

_ACTION_TYPES: dict[CorporateActionKind, CanonicalCorporateActionType] = {
    CorporateActionKind.SPLIT: CanonicalCorporateActionType.SPLIT,
    CorporateActionKind.DIVIDEND: CanonicalCorporateActionType.DIVIDEND,
    CorporateActionKind.MERGER_ACQUISITION: CanonicalCorporateActionType.MERGER,
}


class TranslationError(ValueError):
    """A provider record cannot be translated without guessing.

    Raised rather than defaulted, because every plausible default here
    would be a silent PIT lie. A fundamentals row with no accepted date
    and no filing date has no honest `observation_time`; inventing one
    from the period end is precisely the leakage this module exists to
    prevent, so the row is rejected and reported instead.
    """


@dataclass(slots=True)
class TranslationReport:
    """Records that could not be translated, and why."""

    rejected: list[tuple[str, str]] = field(default_factory=list)

    def reject(self, identifier: str, reason: str) -> None:
        self.rejected.append((identifier, reason))

    def __bool__(self) -> bool:
        return not self.rejected


def _lineage(provenance: FetchProvenance) -> SourceLineage:
    return SourceLineage(
        provider=provenance.provider,
        endpoint=provenance.endpoint,
        from_cache=provenance.from_cache,
    )


def _midnight_utc(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


# --------------------------------------------------------------------------
# OHLCV
# --------------------------------------------------------------------------


def translate_daily_bar(
    bar: DailyBar,
    security_id: UUID,
    *,
    lag: ProviderLagPolicy = DEFAULT_LAG_POLICY,
) -> CanonicalOhlcvBar:
    """Translate one FMP daily bar.

    `event_time` and `observation_time` are both the session close: a
    daily bar describes a session, and the session's closing price is
    exactly what makes the bar complete and knowable. There is no gap
    between the event and its observability the way there is for a
    filing.

    Adjusted values are NOT taken from FMP here. Module 05 recomputes
    them from the corporate actions it has stored
    (`data.normalization.adjustments`), so the adjustment is reproducible
    and auditable — a vendor's adjusted series can change silently
    between fetches, which would break reproducibility. FMP's own
    adjusted values remain available on the source record for
    cross-checking.
    """
    if bar.bar_date == date.min:
        raise TranslationError(f"Bar for {bar.symbol!r} has no usable date.")
    if bar.open is None or bar.high is None or bar.low is None or bar.close is None:
        raise TranslationError(f"Bar {bar.symbol!r} {bar.bar_date} is missing a price field.")

    close_time = session_close(bar.bar_date)
    return CanonicalOhlcvBar(
        security_id=security_id,
        timeframe=CanonicalTimeframe.DAILY,
        pit=PitTimestamps.derive(
            event_time=close_time,
            observation_time=close_time,
            ingestion_time=bar.provenance.fetched_at,
            lag=lag.daily_bar,
        ),
        lineage=_lineage(bar.provenance),
        open_raw=bar.open,
        high_raw=bar.high,
        low_raw=bar.low,
        close_raw=bar.close,
        volume_raw=bar.volume or 0,
    )


# --------------------------------------------------------------------------
# Fundamentals — the critical one
# --------------------------------------------------------------------------


def translate_fundamental(
    statement: FinancialStatement,
    security_id: UUID,
    *,
    lag: ProviderLagPolicy = DEFAULT_LAG_POLICY,
) -> CanonicalFundamental:
    """Translate one financial statement.

    **`observation_time` comes from `accepted_date`, never from the
    fiscal period end.** A Q1 ending March 31 may not be filed until
    May 15; sourcing `observation_time` from the period end would let a
    backtest read those numbers six weeks before they existed, and
    nothing downstream would ever notice.

    Fallback order, and where it stops:

    1. `accepted_date` — when the filing was accepted. Correct.
    2. `filing_date` — coarser (a date, not a timestamp) but still a real
       publication event. Anchored to end-of-day so a same-day backtest
       cannot read it in the morning.
    3. Nothing. The row is rejected. There is deliberately no fall back
       to the period end: that value is always available and always
       wrong, so allowing it as a default would quietly reintroduce the
       exact bug this ordering exists to prevent.
    """
    if statement.fiscal_date is None:
        raise TranslationError(f"Statement {statement.symbol!r} has no fiscal period end.")

    observation_time = _fundamentals_observation_time(statement)
    statement_type = _STATEMENT_TYPES.get(statement.statement_type)
    if statement_type is None:
        raise TranslationError(
            f"Unknown statement type {statement.statement_type!r} for {statement.symbol!r}."
        )

    return CanonicalFundamental(
        security_id=security_id,
        statement_type=statement_type,
        fiscal_period=statement.period or "UNKNOWN",
        fiscal_period_end=statement.fiscal_date,
        reported_currency=statement.reported_currency,
        pit=PitTimestamps.derive(
            # The real-world event: the fiscal period closed.
            event_time=_midnight_utc(statement.fiscal_date),
            # When the numbers first existed publicly.
            observation_time=observation_time,
            ingestion_time=statement.provenance.fetched_at,
            lag=lag.fundamentals,
        ),
        lineage=_lineage(statement.provenance),
        data=dict(statement.data),
    )


def _fundamentals_observation_time(statement: FinancialStatement) -> datetime:
    if statement.accepted_date is not None:
        value = statement.accepted_date
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    if statement.filing_date is not None:
        # End of the filing day: a date alone does not say what time the
        # filing landed, and assuming midnight would make it readable
        # a full day early.
        return datetime.combine(statement.filing_date, time.max, tzinfo=UTC)

    raise TranslationError(
        f"Statement {statement.symbol!r} for period ending {statement.fiscal_date} has neither "
        "acceptedDate nor filingDate. Refusing to fall back to the fiscal period end, which "
        "would make the figures readable weeks before they were filed."
    )


# --------------------------------------------------------------------------
# Corporate actions
# --------------------------------------------------------------------------


def translate_corporate_action(
    action: CorporateAction,
    security_id: UUID,
    *,
    lag: ProviderLagPolicy = DEFAULT_LAG_POLICY,
) -> CanonicalCorporateAction:
    """Translate one corporate action.

    `event_time` is the effective/ex date. `observation_time` is the
    declaration date when the provider supplies one — dividends carry
    `declarationDate`, and a declared dividend is public knowledge from
    that moment.

    Splits as FMP reports them carry no announcement date, so
    `observation_time` falls back to the effective date. That is
    deliberately conservative: a split announced two weeks early is
    treated as knowable only on the day it takes effect, so ARGUS never
    claims foreknowledge it cannot evidence. The cost is confined to the
    *adjusted* series, which exists for cross-corporate-action continuity
    rather than point-in-time realism — the raw series, which is what a
    trader would actually have seen, is unaffected.
    """
    if action.event_date is None:
        raise TranslationError(f"Corporate action for {action.symbol!r} has no date.")

    action_type = _ACTION_TYPES.get(action.kind)
    if action_type is None:
        raise TranslationError(f"Unknown corporate action kind {action.kind!r}.")

    effective = _midnight_utc(action.event_date)
    declared = _declaration_time(action.details)

    return CanonicalCorporateAction(
        security_id=security_id,
        action_type=action_type,
        effective_date=action.event_date,
        pit=PitTimestamps.derive(
            event_time=effective,
            observation_time=declared or effective,
            ingestion_time=action.provenance.fetched_at,
            lag=lag.corporate_action,
        ),
        lineage=_lineage(action.provenance),
        details=dict(action.details),
    )


def _declaration_time(details: dict[str, Any]) -> datetime | None:
    raw = details.get("declarationDate") or details.get("announcementDate")
    if not raw:
        return None
    try:
        return datetime.combine(date.fromisoformat(str(raw)[:10]), time.max, tzinfo=UTC)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------


def translate_news(
    article: NewsArticle,
    security_id: UUID,
    *,
    lag: ProviderLagPolicy = DEFAULT_LAG_POLICY,
) -> CanonicalNewsArticle:
    """Translate one news article.

    Publication is both the event and the moment it became knowable, so
    `event_time` and `observation_time` are the same instant.

    NOTE: Module 03 defines no news table, so nothing persists these —
    see the module README.
    """
    if article.published_at is None:
        raise TranslationError("News article has no publication timestamp.")
    if not article.title:
        raise TranslationError("News article has no headline.")

    published = (
        article.published_at
        if article.published_at.tzinfo
        else article.published_at.replace(tzinfo=UTC)
    )
    return CanonicalNewsArticle(
        security_id=security_id,
        pit=PitTimestamps.derive(
            event_time=published,
            observation_time=published,
            ingestion_time=article.provenance.fetched_at,
            lag=lag.news,
        ),
        lineage=_lineage(article.provenance),
        headline=article.title,
        published_at=published,
        source_site=article.site,
        url=article.url,
        summary=article.text,
    )


def split_ratio(details: dict[str, Any]) -> Decimal | None:
    """The `new shares per old share` ratio from a split's details.

    Returns None when the payload does not describe a usable ratio, so
    the caller can flag the action rather than silently applying a
    factor of 1 and producing a discontinuous adjusted series.
    """
    numerator = details.get("numerator")
    denominator = details.get("denominator")
    if numerator is None or denominator is None:
        return None
    try:
        num = Decimal(str(numerator))
        den = Decimal(str(denominator))
    except (ArithmeticError, ValueError):
        return None
    if num <= 0 or den <= 0:
        return None
    return num / den
