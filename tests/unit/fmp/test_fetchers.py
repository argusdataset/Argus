"""Parsing and fetch behaviour, against recorded fixtures."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from data.provider_adapters.fmp.errors import FmpProviderError
from data.provider_adapters.fmp.fetchers import PROFILE_RESOLUTION_KEY
from data.provider_adapters.fmp.models import CorporateActionKind, EmptyReason
from tests.unit.fmp.conftest import fixture_handler, load_fixture


async def test_fetches_full_stock_list(make_fetcher):
    fetcher, client = make_fetcher(
        fixture_handler({"/stock-list": load_fixture("stock_list.json")})
    )
    async with client:
        result = await fetcher.fetch_stock_list()

    assert len(result) == 4
    assert {record.symbol for record in result.records} == {"AAPL", "GE", "SHOP.TO", "SPY"}


async def test_exchange_listings_filter_to_nyse_and_nasdaq(make_fetcher):
    """The universe is NYSE + NASDAQ — no hardcoded list, no fixed count."""
    fetcher, client = make_fetcher(
        fixture_handler({"/stock-list": load_fixture("stock_list.json")})
    )
    async with client:
        result = await fetcher.fetch_exchange_listings(("NYSE", "NASDAQ"))

    assert {record.symbol for record in result.records} == {"AAPL", "GE"}


async def test_unmodelled_fields_are_preserved_not_dropped(make_fetcher):
    """Module 05 decides what matters; the adapter must not lose data first."""
    fetcher, client = make_fetcher(
        fixture_handler({"/stock-list": load_fixture("stock_list.json")})
    )
    async with client:
        result = await fetcher.fetch_stock_list()

    apple = next(record for record in result.records if record.symbol == "AAPL")
    assert apple.raw["price"] == 189.5


# --- /stable/profile, the one listing endpoint a free key can reach ------
#
# FMP's documentation does not pin this payload's field names, and the
# ones it does return are *not* stock-list's: the long venue name arrives
# under `exchangeFullName` and the short one under plain `exchange`,
# where stock-list uses `exchange` and `exchangeShortName`. A wrong guess
# would not error — the venue would fall to UNKNOWN and the security
# would be excluded as if it were listed somewhere ARGUS does not cover,
# which sends the reader looking at exchanges instead of at field names.


PROFILE_ROW = {
    "symbol": "AAPL",
    "companyName": "Apple Inc.",
    "exchangeFullName": "NASDAQ Global Select",
    "exchange": "NASDAQ",
    "ipoDate": "1980-12-12",
}


async def test_a_profile_becomes_a_listing_with_both_venue_labels(make_fetcher):
    fetcher, client = make_fetcher(fixture_handler({"/profile": [PROFILE_ROW]}))
    async with client:
        result = await fetcher.fetch_company_profile("AAPL")

    listing = result.records[0]
    assert listing.symbol == "AAPL"
    assert listing.name == "Apple Inc."
    assert listing.exchange == "NASDAQ Global Select"
    assert listing.exchange_short_name == "NASDAQ"


async def test_the_stock_list_spelling_also_resolves(make_fetcher):
    """The aliases cover both shapes, so either payload works here."""
    row = {
        "symbol": "GE",
        "name": "General Electric",
        "exchange": "New York Stock Exchange",
        "exchangeShortName": "NYSE",
    }
    fetcher, client = make_fetcher(fixture_handler({"/profile": [row]}))
    async with client:
        result = await fetcher.fetch_company_profile("GE")

    listing = result.records[0]
    assert listing.exchange == "New York Stock Exchange"
    assert listing.exchange_short_name == "NYSE"


async def test_the_record_says_which_spelling_resolved(make_fetcher):
    """So an unexpected payload is readable from the record itself."""
    fetcher, client = make_fetcher(fixture_handler({"/profile": [PROFILE_ROW]}))
    async with client:
        result = await fetcher.fetch_company_profile("AAPL")

    resolution = result.records[0].raw[PROFILE_RESOLUTION_KEY]
    assert resolution["resolved"]["exchange"] == "exchangeFullName"
    assert resolution["resolved"]["exchange_short_name"] == "exchange"
    assert resolution["unresolved"] == ["security_type"]


async def test_an_unrecognisable_symbol_field_is_a_named_failure(make_fetcher):
    """Not a listing with an empty symbol, which would be excluded
    downstream as an unrecognised venue — the wrong problem entirely."""
    fetcher, client = make_fetcher(fixture_handler({"/profile": [{"Symbol": "AAPL"}]}))
    async with client:
        with pytest.raises(FmpProviderError) as failure:
            await fetcher.fetch_company_profile("AAPL")

    assert "symbol, ticker" in str(failure.value)
    assert "Symbol" in str(failure.value)


async def test_a_symbol_fmp_does_not_carry_returns_no_records(make_fetcher):
    """FMP answers an unknown ticker with `[]` rather than a 404."""
    fetcher, client = make_fetcher(fixture_handler({"/profile": []}))
    async with client:
        result = await fetcher.fetch_company_profile("NOSUCHTICKER")

    assert result.records == []
    assert result.empty_reason is EmptyReason.NO_DATA_RETURNED


async def test_the_profile_keeps_fields_the_adapter_does_not_model(make_fetcher):
    """`ipoDate` is better listing evidence than the observation instant.

    It is kept rather than used: consuming it means changing
    `_observe_listing`, which the whole-market path shares. See
    `build_intervals_from_symbols` on why that trade was refused.
    """
    fetcher, client = make_fetcher(fixture_handler({"/profile": [PROFILE_ROW]}))
    async with client:
        result = await fetcher.fetch_company_profile("AAPL")

    assert result.records[0].raw["ipoDate"] == "1980-12-12"


async def test_parses_daily_bars_with_raw_and_adjusted_prices(make_fetcher):
    """ARGUS stores both; collapsing them here would lose one irrecoverably."""
    fetcher, client = make_fetcher(
        fixture_handler(
            {"/historical-price-eod/full": load_fixture("historical_price_eod_full.json")}
        )
    )
    async with client:
        result = await fetcher.fetch_daily_history("AAPL")

    assert len(result) == 2
    bar = result.records[0]
    assert bar.bar_date == date(2024, 1, 3)
    assert bar.close == Decimal("184.25")
    assert bar.adjusted_close == Decimal("183.63")
    assert bar.volume == 58414500


async def test_prices_are_decimals_not_floats(make_fetcher):
    """Binary floats accumulate error across a 30-year series."""
    fetcher, client = make_fetcher(
        fixture_handler(
            {"/historical-price-eod/full": load_fixture("historical_price_eod_full.json")}
        )
    )
    async with client:
        result = await fetcher.fetch_daily_history("AAPL")

    assert isinstance(result.records[0].close, Decimal)


async def test_fetches_delisted_companies_for_survivorship_resistance(make_fetcher):
    def handler(request):
        # Second page empty, so paging terminates.
        if request.url.params.get("page") == "0":
            return fixture_handler(
                {"/delisted-companies": load_fixture("delisted_companies.json")}
            )(request)
        return fixture_handler({"/delisted-companies": []})(request)

    fetcher, client = make_fetcher(handler)
    async with client:
        result = await fetcher.fetch_delisted_companies(page_size=100)

    assert {record.symbol for record in result.records} == {"LEHMQ", "SBUXQ"}
    assert result.records[0].delisted_date == date(2008, 9, 17)


async def test_delisting_reason_is_not_available_from_fmp(make_fetcher):
    """Recorded as a test because Module 09's bankruptcy gate depends on it.

    FMP supplies the delisting date and exchange but not the reason, so a
    bankruptcy and an acquisition are indistinguishable here.
    """
    fetcher, client = make_fetcher(
        fixture_handler({"/delisted-companies": load_fixture("delisted_companies.json")})
    )
    async with client:
        result = await fetcher.fetch_delisted_companies(max_pages=1)

    record = result.records[0]
    assert not hasattr(record, "delisting_reason")
    assert "reason" not in record.raw


async def test_parses_bulk_eod_csv(make_fetcher):
    """The bulk endpoint returns CSV, not JSON."""
    fetcher, client = make_fetcher(fixture_handler({"/eod-bulk": load_fixture("eod_bulk.csv")}))
    async with client:
        result = await fetcher.fetch_eod_for_date(date(2024, 1, 3))

    assert {record.symbol for record in result.records} == {"AAPL", "GE"}
    assert result.records[0].close == Decimal("184.25")


async def test_statement_line_items_stay_in_data_payload(make_fetcher):
    """Module 05 owns the canonical fundamentals taxonomy."""
    fetcher, client = make_fetcher(
        fixture_handler({"/income-statement": load_fixture("income_statement.json")})
    )
    async with client:
        result = await fetcher.fetch_financial_statement("AAPL", "INCOME_STATEMENT")

    statement = result.records[0]
    assert statement.fiscal_date == date(2023, 9, 30)
    assert statement.data["revenue"] == 383285000000
    # accepted_date is what stops a backtest assuming the numbers were
    # known at fiscal period end.
    assert statement.accepted_date is not None


async def test_unknown_statement_type_is_rejected(make_fetcher):
    fetcher, client = make_fetcher(fixture_handler({}))
    async with client:
        with pytest.raises(ValueError, match="Unknown statement_type"):
            await fetcher.fetch_financial_statement("AAPL", "NOT_A_STATEMENT")


async def test_fetches_splits_and_dividends_without_applying_them(make_fetcher):
    """Adjustment is Module 05's job — this fetches the events only."""
    fetcher, client = make_fetcher(
        fixture_handler(
            {"/splits": load_fixture("splits.json"), "/dividends": load_fixture("dividends.json")}
        )
    )
    async with client:
        splits = await fetcher.fetch_splits("AAPL")
        dividends = await fetcher.fetch_dividends("AAPL")

    assert splits.records[0].kind is CorporateActionKind.SPLIT
    assert splits.records[0].details["numerator"] == 4
    assert dividends.records[0].kind is CorporateActionKind.DIVIDEND
    assert dividends.records[0].details["dividend"] == 0.24


async def test_fetches_earnings_calendar(make_fetcher):
    fetcher, client = make_fetcher(
        fixture_handler({"/earnings-calendar": load_fixture("earnings_calendar.json")})
    )
    async with client:
        result = await fetcher.fetch_earnings_calendar(date(2024, 4, 1), date(2024, 5, 31))

    assert {record.symbol for record in result.records} == {"AAPL", "MSFT"}
    assert result.records[0].earnings_date == date(2024, 5, 2)
    assert result.records[0].timing == "amc"


async def test_fetches_news(make_fetcher):
    fetcher, client = make_fetcher(
        fixture_handler({"/news/stock": load_fixture("stock_news.json")})
    )
    async with client:
        result = await fetcher.fetch_news(["AAPL"])

    assert result.records[0].title == "Apple beats expectations"


async def test_empty_response_reports_a_reason_rather_than_looking_like_success(make_fetcher):
    """An empty result must be distinguishable from a populated one."""
    fetcher, client = make_fetcher(fixture_handler({"/historical-price-eod/full": []}))
    async with client:
        result = await fetcher.fetch_daily_history("NOSUCH")

    assert not result
    assert result.empty_reason is EmptyReason.NO_DATA_RETURNED
