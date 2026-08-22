"""Point-in-time timestamp sourcing — the leakage boundary.

The fundamentals test in this file is the single most important test in
Module 05. Everything else ARGUS claims rests on historical calculations
not having seen the future, and a filing's numbers exist weeks after the
period they describe closes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from data.canonical_model.pit import DEFAULT_LAG_POLICY, PitTimestamps, session_close
from data.normalization.translate import (
    TranslationError,
    translate_corporate_action,
    translate_daily_bar,
    translate_fundamental,
    translate_news,
)
from data.provider_adapters.fmp.models import (
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    FetchProvenance,
    FinancialStatement,
    NewsArticle,
)

SECURITY_ID = uuid4()
FETCHED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def provenance(endpoint: str = "income_statement", *, from_cache: bool = False) -> FetchProvenance:
    return FetchProvenance(
        endpoint=endpoint,
        url_path=f"/stable/{endpoint}",
        fetched_at=FETCHED_AT,
        from_cache=from_cache,
    )


# --------------------------------------------------------------------------
# Fundamentals: acceptedDate, never fiscal period end
# --------------------------------------------------------------------------


def _statement(**overrides) -> FinancialStatement:
    values = {
        "provenance": provenance(),
        "symbol": "AAPL",
        "statement_type": "INCOME_STATEMENT",
        # Q1 ends March 31...
        "fiscal_date": date(2024, 3, 31),
        "period": "Q1",
        # ...but is not accepted until May 15, six weeks later.
        "accepted_date": datetime(2024, 5, 15, 18, 8, 27, tzinfo=UTC),
        "filing_date": date(2024, 5, 16),
        "data": {"revenue": 90753000000},
    }
    values.update(overrides)
    return FinancialStatement(**values)


def test_observation_time_uses_accepted_date_not_period_end():
    """THE test for this module.

    If observation_time were the fiscal period end, every backtest could
    read Q1 numbers on March 31 — six weeks before they existed. Nothing
    downstream would ever notice.
    """
    canonical = translate_fundamental(_statement(), SECURITY_ID)

    assert canonical.pit.observation_time == datetime(2024, 5, 15, 18, 8, 27, tzinfo=UTC)
    # And emphatically not the period end.
    assert canonical.pit.observation_time != datetime(2024, 3, 31, tzinfo=UTC)
    assert canonical.pit.observation_time.date() != canonical.fiscal_period_end


def test_event_time_is_the_fiscal_period_end():
    """The real-world event is the period closing — that part is unchanged."""
    canonical = translate_fundamental(_statement(), SECURITY_ID)

    assert canonical.pit.event_time == datetime(2024, 3, 31, tzinfo=UTC)
    assert canonical.fiscal_period_end == date(2024, 3, 31)


def test_observation_time_is_six_weeks_after_event_time():
    """Stated as a duration, because that gap is the whole hazard."""
    canonical = translate_fundamental(_statement(), SECURITY_ID)

    gap = canonical.pit.observation_time - canonical.pit.event_time
    assert gap > timedelta(days=40)


def test_falls_back_to_filing_date_when_accepted_date_is_missing():
    canonical = translate_fundamental(_statement(accepted_date=None), SECURITY_ID)

    # End of the filing day: a bare date does not say when it landed, and
    # assuming midnight would make it readable a full day early.
    assert canonical.pit.observation_time.date() == date(2024, 5, 16)
    assert canonical.pit.observation_time.hour == 23


def test_statement_with_no_publication_date_is_rejected_not_defaulted():
    """The fallback chain deliberately stops rather than using period end.

    Period end is always available and always wrong. Allowing it as a
    last resort would quietly reintroduce the exact bug the ordering
    exists to prevent.
    """
    with pytest.raises(TranslationError, match="acceptedDate"):
        translate_fundamental(_statement(accepted_date=None, filing_date=None), SECURITY_ID)


def test_availability_time_is_after_observation_time():
    canonical = translate_fundamental(_statement(), SECURITY_ID)

    assert canonical.pit.availability_time > canonical.pit.observation_time
    assert (
        canonical.pit.availability_time - canonical.pit.observation_time
        == DEFAULT_LAG_POLICY.fundamentals
    )


def test_ingestion_time_is_the_providers_fetch_time():
    canonical = translate_fundamental(_statement(), SECURITY_ID)

    assert canonical.pit.ingestion_time == FETCHED_AT


def test_cache_hit_keeps_the_original_fetch_time_as_ingestion_time():
    """Module 04 guarantees fetched_at survives a cache read; verify it carries."""
    canonical = translate_fundamental(
        _statement(provenance=provenance(from_cache=True)), SECURITY_ID
    )

    assert canonical.pit.ingestion_time == FETCHED_AT
    assert canonical.lineage.from_cache is True


# --------------------------------------------------------------------------
# OHLCV
# --------------------------------------------------------------------------


def _bar(**overrides) -> DailyBar:
    values = {
        "provenance": provenance("historical_price_eod_full"),
        "symbol": "AAPL",
        "bar_date": date(2024, 1, 3),
        "open": Decimal("184.22"),
        "high": Decimal("185.88"),
        "low": Decimal("183.43"),
        "close": Decimal("184.25"),
        "volume": 58414500,
    }
    values.update(overrides)
    return DailyBar(**values)


def test_bar_event_time_is_the_session_close_not_utc_midnight():
    """A bar is knowable at the close, and the close is 16:00 New York."""
    canonical = translate_daily_bar(_bar(), SECURITY_ID)

    # 2024-01-03 is EST (UTC-5), so 16:00 local is 21:00 UTC.
    assert canonical.pit.event_time == datetime(2024, 1, 3, 21, 0, tzinfo=UTC)


def test_session_close_follows_daylight_saving():
    """Anchoring to New York rather than a fixed offset avoids a twice-yearly hour of drift."""
    winter = session_close(date(2024, 1, 3))
    summer = session_close(date(2024, 7, 3))

    assert winter.hour == 21  # EST, UTC-5
    assert summer.hour == 20  # EDT, UTC-4


def test_bar_observation_time_equals_its_event_time():
    """Unlike a filing, a bar has no gap between happening and being knowable."""
    canonical = translate_daily_bar(_bar(), SECURITY_ID)

    assert canonical.pit.observation_time == canonical.pit.event_time


def test_bar_availability_time_lags_the_close():
    canonical = translate_daily_bar(_bar(), SECURITY_ID)

    assert canonical.pit.availability_time > canonical.pit.event_time


def test_bar_missing_a_price_is_rejected():
    with pytest.raises(TranslationError, match="missing a price"):
        translate_daily_bar(_bar(close=None), SECURITY_ID)


# --------------------------------------------------------------------------
# Corporate actions
# --------------------------------------------------------------------------


def test_dividend_observation_time_uses_the_declaration_date():
    """A declared dividend is public from the declaration, not the ex-date."""
    action = CorporateAction(
        provenance=provenance("dividends"),
        symbol="AAPL",
        kind=CorporateActionKind.DIVIDEND,
        event_date=date(2024, 2, 9),
        details={"dividend": 0.24, "declarationDate": "2024-02-01"},
    )
    canonical = translate_corporate_action(action, SECURITY_ID)

    assert canonical.pit.observation_time.date() == date(2024, 2, 1)
    assert canonical.pit.event_time.date() == date(2024, 2, 9)
    assert canonical.pit.observation_time < canonical.pit.event_time


def test_split_without_an_announcement_date_falls_back_to_the_effective_date():
    """Conservative: never claim foreknowledge that cannot be evidenced.

    FMP's split payload carries no announcement date. Treating the split
    as knowable only when it takes effect errs late, which is the safe
    direction — it can make ARGUS slightly pessimistic, never leaky.
    """
    action = CorporateAction(
        provenance=provenance("splits"),
        symbol="AAPL",
        kind=CorporateActionKind.SPLIT,
        event_date=date(2020, 8, 31),
        details={"numerator": 4, "denominator": 1},
    )
    canonical = translate_corporate_action(action, SECURITY_ID)

    assert canonical.pit.observation_time == canonical.pit.event_time


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------


def test_news_observation_time_is_publication_time():
    article = NewsArticle(
        provenance=provenance("stock_news"),
        symbol="AAPL",
        published_at=datetime(2024, 5, 3, 9, 31, tzinfo=UTC),
        title="Apple beats expectations",
        site="example.com",
    )
    canonical = translate_news(article, SECURITY_ID)

    assert canonical.pit.event_time == canonical.pit.observation_time
    assert canonical.pit.observation_time == datetime(2024, 5, 3, 9, 31, tzinfo=UTC)


# --------------------------------------------------------------------------
# PitTimestamps invariants
# --------------------------------------------------------------------------


def test_availability_time_is_never_before_observation_time():
    """A provider cannot serve what has not been observed yet."""
    pit = PitTimestamps.derive(
        event_time=datetime(2024, 1, 1, tzinfo=UTC),
        observation_time=datetime(2024, 1, 10, tzinfo=UTC),
        ingestion_time=datetime(2024, 2, 1, tzinfo=UTC),
        lag=timedelta(hours=1),
        availability_time=datetime(2023, 1, 1, tzinfo=UTC),  # nonsense
    )

    assert pit.availability_time == pit.observation_time


def test_naive_datetimes_are_treated_as_utc_not_local_time():
    """Process timezone is a deployment accident and must not change meaning."""
    pit = PitTimestamps.derive(
        event_time=datetime(2024, 1, 1, 12, 0),
        observation_time=datetime(2024, 1, 1, 12, 0),
        ingestion_time=datetime(2024, 1, 2, 12, 0),
        lag=timedelta(hours=1),
    )

    assert pit.event_time == datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


def test_ingestion_time_may_far_exceed_availability_time():
    """The normal case for a backfill: fetching 2010 data in 2026."""
    pit = PitTimestamps.derive(
        event_time=datetime(2010, 1, 1, tzinfo=UTC),
        observation_time=datetime(2010, 1, 1, tzinfo=UTC),
        ingestion_time=datetime(2026, 8, 22, tzinfo=UTC),
        lag=timedelta(hours=16),
    )

    assert pit.ingestion_time.year == 2026
    assert pit.availability_time.year == 2010
