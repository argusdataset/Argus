"""End-to-end universe construction against a real database.

Runs Module 04's real fetch path (MockTransport, no live calls), Module
05's real identity resolution, and Module 03's real append-only guards.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.universe.admission import ExclusionReason
from core.universe.builder import bar_date_bounds, build_intervals_from_fetch, construct_version
from core.universe.repository import UniverseRepository
from data.canonical_model.exchanges import CanonicalExchange
from data.canonical_model.pit import session_close
from data.normalization.identity import SecurityIdentityResolver
from data.normalization.persistence import CanonicalWriter
from data.normalization.pipeline import normalize_security, persist
from data.provider_adapters.fmp.models import DailyBar, FetchProvenance
from tests.unit.fmp.conftest import fixture_handler

OBSERVED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

#: A deliberately mixed listing feed: two universe venues, one Arca ETF
#: with FMP's inconsistent labelling, one foreign listing, one venue label
#: `normalize_exchange` does not recognise.
STOCK_LIST = [
    {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "exchange": "NASDAQ Global Select",
        "exchangeShortName": "NASDAQ",
    },
    {
        "symbol": "GE",
        "name": "General Electric",
        "exchange": "New York Stock Exchange",
        "exchangeShortName": "NYSE",
    },
    {
        "symbol": "SPY",
        "name": "SPDR S&P 500 ETF",
        "exchange": "NYSE Arca",
        "exchangeShortName": "AMEX",
    },
    {
        "symbol": "SHOP.TO",
        "name": "Shopify",
        "exchange": "Toronto",
        "exchangeShortName": "TSX",
    },
    {
        "symbol": "MYSTERY",
        "name": "Unknown Venue Co",
        "exchange": "Bucharest Stock Exchange",
        "exchangeShortName": "BVB",
    },
    # Label says NYSE, ticker says London. A provider data error, and the
    # only row that exercises the symbol-suffix guard — SHOP.TO above is
    # already caught earlier by its unrecognised "Toronto" label.
    {
        "symbol": "FAKE.L",
        "name": "Mislabelled Foreign Listing",
        "exchange": "New York Stock Exchange",
        "exchangeShortName": "NYSE",
    },
]

DELISTED = [
    {
        "symbol": "LEHMQ",
        "companyName": "Lehman Brothers Holdings",
        "exchange": "NYSE",
        "ipoDate": "1994-05-06",
        "delistedDate": "2008-09-17",
    },
    {
        "symbol": "ENRNQ",
        "companyName": "Enron Corp",
        "exchange": "NYSE",
        "ipoDate": "1985-07-01",
        "delistedDate": "2002-01-15",
    },
]


def universe_handler(stock_list=None, delisted=None):
    """Serve listings, then an empty delisted page so paging terminates."""
    listings = STOCK_LIST if stock_list is None else stock_list
    delistings = DELISTED if delisted is None else delisted

    def handler(request):
        if request.url.path.endswith("/delisted-companies"):
            page = request.url.params.get("page", "0")
            payload = delistings if page == "0" else []
            return fixture_handler({"/delisted-companies": payload})(request)
        return fixture_handler({"/stock-list": listings})(request)

    return handler


async def _construct(make_fetcher, connection, *, as_of=None, **kwargs):
    fetcher, client = make_fetcher(universe_handler(**kwargs))
    resolver = SecurityIdentityResolver(connection)
    async with client:
        construction = await build_intervals_from_fetch(fetcher, resolver, observed_at=OBSERVED_AT)
    repository = UniverseRepository(connection)
    version = construct_version(construction, repository, as_of=as_of or OBSERVED_AT)
    return construction, version, repository


# --------------------------------------------------------------------------
# Today's universe
# --------------------------------------------------------------------------


async def test_constructs_todays_universe_from_the_exchange_feed(make_fetcher, connection):
    construction, version, repository = await _construct(make_fetcher, connection)

    # AAPL and GE only. SPY is Arca; SHOP.TO and MYSTERY have venue labels
    # normalize_exchange does not recognise; FAKE.L is caught by its ticker
    # suffix despite an NYSE label.
    assert version.member_count == 2
    exchanges = {row["exchange"] for row in repository.members(version.id)}
    assert exchanges == {"NASDAQ", "NYSE"}


async def test_universe_size_is_whatever_the_feed_reports(make_fetcher, connection):
    """No hardcoded count anywhere: a bigger feed gives a bigger universe."""
    bigger = STOCK_LIST + [
        {
            "symbol": f"TEST{index}",
            "name": f"Test {index}",
            "exchange": "NASDAQ Capital Market",
            "exchangeShortName": "NASDAQ",
        }
        for index in range(7)
    ]
    _, version, _ = await _construct(make_fetcher, connection, stock_list=bigger)

    assert version.member_count == 9


async def test_exchange_classification_uses_module_05s_normalizer(make_fetcher, connection):
    """SPY's labels disagree; the long name wins, so it is Arca, not admitted."""
    construction, _, _ = await _construct(make_fetcher, connection)

    excluded = construction.admission.samples[ExclusionReason.NON_UNIVERSE_EXCHANGE]
    assert "SPY" in {item.symbol for item in excluded}


# --------------------------------------------------------------------------
# Reporting, not silent dropping
# --------------------------------------------------------------------------


async def test_unknown_exchange_securities_are_reported_never_dropped_silently(
    make_fetcher, connection
):
    """Module 05's explicit instruction to this module."""
    construction, _, _ = await _construct(make_fetcher, connection)
    report = construction.admission

    # SHOP.TO ("Toronto") and MYSTERY ("Bucharest Stock Exchange").
    assert report.unknown_exchange_count == 2
    assert "Bucharest Stock Exchange" in report.unknown_exchange_labels
    assert "Toronto" in report.unknown_exchange_labels
    assert "MYSTERY" in {item.symbol for item in report.samples[ExclusionReason.UNKNOWN_EXCHANGE]}


async def test_a_foreign_ticker_with_a_us_label_is_caught_by_its_suffix(make_fetcher, connection):
    """The secondary guard: label and ticker disagree, so the ticker wins."""
    construction, _, _ = await _construct(make_fetcher, connection)

    excluded = construction.admission.samples[ExclusionReason.FOREIGN_SYMBOL_SUFFIX]
    assert {item.symbol for item in excluded} == {"FAKE.L"}


async def test_unknown_securities_do_not_enter_the_universe(make_fetcher, connection):
    _, version, repository = await _construct(make_fetcher, connection)

    symbols = (
        connection.execute(
            text(
                "SELECT h.ticker FROM universe_membership m "
                "JOIN security_ticker_history h ON h.security_id = m.security_id "
                "WHERE m.universe_version_id = :vid"
            ),
            {"vid": version.id},
        )
        .scalars()
        .all()
    )

    assert "MYSTERY" not in symbols
    assert "SHOP.TO" not in symbols


async def test_the_admission_summary_is_stored_on_the_version(make_fetcher, connection):
    """So an unexplained universe shrink is diagnosable without a refetch."""
    _, version, _ = await _construct(make_fetcher, connection)

    definition = connection.execute(
        text("SELECT definition FROM universe_version WHERE id = :vid"), {"vid": version.id}
    ).scalar_one()

    assert definition["admission"]["excluded_by_reason"]["unknown_exchange"] == 2
    assert definition["admission"]["excluded_by_reason"]["foreign_symbol_suffix"] == 1
    assert definition["member_count"] == 2


async def test_a_tier_rename_still_resolves(make_fetcher, connection):
    """normalize_exchange matches on substring, so tier renames are survivable.

    "NASDAQ Global Select" becoming "NASDAQ Tier Renamed" still contains
    "nasdaq" and still classifies. Worth pinning: this is why a routine
    relabelling does not shrink the universe.
    """
    renamed = [
        {**row, "exchange": "NASDAQ Tier Renamed", "exchangeShortName": "XXXX"}
        if row["symbol"] == "AAPL"
        else row
        for row in STOCK_LIST
    ]
    construction, version, _ = await _construct(make_fetcher, connection, stock_list=renamed)

    assert construction.admission.unknown_exchange_count == 2
    assert version.member_count == 2


async def test_a_wholly_unrecognised_venue_label_shows_up_as_an_unknown_spike(
    make_fetcher, connection
):
    """The failure mode the report exists to make visible.

    A rebrand with no recognisable substring drops the security out of the
    universe. Nothing downstream would fail — the scan would just quietly
    cover fewer stocks. The report is what makes it visible, and names the
    label responsible.
    """
    renamed = [
        {**row, "exchange": "US Tech Board", "exchangeShortName": "USTB"}
        if row["symbol"] == "AAPL"
        else row
        for row in STOCK_LIST
    ]
    construction, version, _ = await _construct(make_fetcher, connection, stock_list=renamed)

    # AAPL joins SHOP.TO and MYSTERY in the unknown bucket.
    assert construction.admission.unknown_exchange_count == 3
    assert "US Tech Board" in construction.admission.unknown_exchange_labels
    # AAPL would have vanished from the universe silently — but the report
    # names the label that caused it.
    assert version.member_count == 1


# --------------------------------------------------------------------------
# Historical universes and survivorship bias
# --------------------------------------------------------------------------


async def test_a_historical_universe_includes_a_since_delisted_security(make_fetcher, connection):
    """The survivorship-bias requirement, end to end."""
    fetcher, client = make_fetcher(universe_handler())
    resolver = SecurityIdentityResolver(connection)
    async with client:
        construction = await build_intervals_from_fetch(fetcher, resolver, observed_at=OBSERVED_AT)
    repository = UniverseRepository(connection)

    historical = construct_version(construction, repository, as_of=datetime(2005, 6, 1, tzinfo=UTC))

    members = repository.members(historical.id)
    statuses = {row["listing_status"] for row in members}
    assert "DELISTED" in statuses

    lehman = connection.execute(
        text(
            "SELECT m.listed_from, m.listed_to, m.listing_status FROM universe_membership m "
            "JOIN security_ticker_history h ON h.security_id = m.security_id "
            "WHERE m.universe_version_id = :vid AND h.ticker = 'LEHMQ'"
        ),
        {"vid": historical.id},
    ).one()
    assert lehman.listed_from.date() == date(1994, 5, 6)
    assert lehman.listed_to.date() == date(2008, 9, 17)


async def test_the_same_security_is_absent_from_todays_universe(make_fetcher, connection):
    """Present in 2005, gone today — which is what a dated join buys."""
    fetcher, client = make_fetcher(universe_handler())
    resolver = SecurityIdentityResolver(connection)
    async with client:
        construction = await build_intervals_from_fetch(fetcher, resolver, observed_at=OBSERVED_AT)
    repository = UniverseRepository(connection)

    historical = construct_version(construction, repository, as_of=datetime(2005, 6, 1, tzinfo=UTC))
    current = construct_version(construction, repository, as_of=OBSERVED_AT)

    def tickers(version_id):
        return set(
            connection.execute(
                text(
                    "SELECT h.ticker FROM universe_membership m "
                    "JOIN security_ticker_history h ON h.security_id = m.security_id "
                    "WHERE m.universe_version_id = :vid"
                ),
                {"vid": version_id},
            ).scalars()
        )

    assert "LEHMQ" in tickers(historical.id)
    assert "LEHMQ" not in tickers(current.id)


async def test_enron_appears_only_before_its_delisting(make_fetcher, connection):
    fetcher, client = make_fetcher(universe_handler())
    resolver = SecurityIdentityResolver(connection)
    async with client:
        construction = await build_intervals_from_fetch(fetcher, resolver, observed_at=OBSERVED_AT)
    repository = UniverseRepository(connection)

    before = construct_version(construction, repository, as_of=datetime(2000, 1, 1, tzinfo=UTC))
    after = construct_version(construction, repository, as_of=datetime(2003, 1, 1, tzinfo=UTC))

    assert repository.member_count(before.id) > repository.member_count(after.id)


# --------------------------------------------------------------------------
# Ticker changes
# --------------------------------------------------------------------------


async def test_a_ticker_change_produces_one_membership_row(make_fetcher, connection):
    """FB and META are one security, so one row, not two."""
    resolver = SecurityIdentityResolver(connection)
    security_id = resolver.register(
        "FB", exchange=CanonicalExchange.NASDAQ, valid_from=datetime(2012, 5, 18, tzinfo=UTC)
    )
    resolver.record_ticker_change(
        old_symbol="FB",
        new_symbol="META",
        changed_at=datetime(2022, 6, 9, tzinfo=UTC),
        exchange=CanonicalExchange.NASDAQ,
    )

    feed = [
        {
            "symbol": "META",
            "name": "Meta Platforms",
            "exchange": "NASDAQ Global Select",
            "exchangeShortName": "NASDAQ",
        }
    ]
    fetcher, client = make_fetcher(universe_handler(stock_list=feed, delisted=[]))
    async with client:
        construction = await build_intervals_from_fetch(fetcher, resolver, observed_at=OBSERVED_AT)
    version = construct_version(construction, UniverseRepository(connection), as_of=OBSERVED_AT)

    rows = (
        connection.execute(
            text("SELECT security_id FROM universe_membership WHERE universe_version_id = :vid"),
            {"vid": version.id},
        )
        .scalars()
        .all()
    )

    assert rows == [security_id]


# --------------------------------------------------------------------------
# Price history as interval evidence
# --------------------------------------------------------------------------


async def test_price_history_dates_a_listing_for_historical_membership(make_fetcher, connection):
    """Without it, a live security cannot be placed in any past universe."""
    resolver = SecurityIdentityResolver(connection)
    security_id = resolver.register(
        "AAPL", exchange=CanonicalExchange.NASDAQ, valid_from=datetime(1980, 12, 12, tzinfo=UTC)
    )

    bar_day = date(2005, 3, 1)
    price = Decimal("40")
    bar = DailyBar(
        provenance=FetchProvenance(
            endpoint="historical_price_eod_full",
            url_path="/stable/historical-price-eod/full",
            fetched_at=OBSERVED_AT,
        ),
        symbol="AAPL",
        bar_date=bar_day,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1_000,
    )
    persist(normalize_security(security_id=security_id, bars=[bar]), CanonicalWriter(connection))

    first_bars, last_bars = bar_date_bounds(connection)
    assert first_bars[security_id] == session_close(bar_day).date()

    feed = [
        {
            "symbol": "AAPL",
            "name": "Apple",
            "exchange": "NASDAQ Global Select",
            "exchangeShortName": "NASDAQ",
        }
    ]
    fetcher, client = make_fetcher(universe_handler(stock_list=feed, delisted=[]))
    async with client:
        construction = await build_intervals_from_fetch(
            fetcher,
            resolver,
            observed_at=OBSERVED_AT,
            first_bar_dates=first_bars,
            last_bar_dates=last_bars,
        )

    repository = UniverseRepository(connection)
    historical = construct_version(construction, repository, as_of=datetime(2006, 1, 1, tzinfo=UTC))

    assert historical.member_count == 1
    assert repository.members(historical.id)[0]["interval_evidence"].startswith(
        "from=price_history"
    )


# --------------------------------------------------------------------------
# Versioning and immutability
# --------------------------------------------------------------------------


async def test_reconstructing_an_unchanged_universe_reuses_the_version(make_fetcher, connection):
    _, first, _ = await _construct(make_fetcher, connection)
    _, second, _ = await _construct(make_fetcher, connection)

    assert second.id == first.id
    assert second.reused is True


async def test_a_changed_universe_creates_a_new_version(make_fetcher, connection):
    _, first, _ = await _construct(make_fetcher, connection)

    extended = STOCK_LIST + [
        {
            "symbol": "NEWCO",
            "name": "New Co",
            "exchange": "NASDAQ Capital Market",
            "exchangeShortName": "NASDAQ",
        }
    ]
    _, second, _ = await _construct(make_fetcher, connection, stock_list=extended)

    assert second.id != first.id
    assert second.reused is False
    assert second.member_count == first.member_count + 1


async def test_a_published_universe_version_cannot_be_updated(make_fetcher, connection):
    """Append-only, consistent with Module 03's pattern."""
    _, version, _ = await _construct(make_fetcher, connection)

    with pytest.raises(IntegrityError) as exc_info:
        connection.execute(
            text("UPDATE universe_version SET description = 'tampered' WHERE id = :vid"),
            {"vid": version.id},
        )
    assert "append-only" in str(exc_info.value)


async def test_a_published_universe_version_cannot_be_deleted(make_fetcher, connection):
    _, version, _ = await _construct(make_fetcher, connection)

    with pytest.raises(IntegrityError):
        connection.execute(
            text("DELETE FROM universe_version WHERE id = :vid"), {"vid": version.id}
        )


async def test_membership_cannot_be_mutated(make_fetcher, connection):
    """Otherwise a signal's recorded universe could change under it."""
    _, version, _ = await _construct(make_fetcher, connection)

    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                "UPDATE universe_membership SET listed_to = now() WHERE universe_version_id = :vid"
            ),
            {"vid": version.id},
        )


async def test_membership_rows_carry_the_interval_that_justifies_them(make_fetcher, connection):
    """The dated join Module 07 will query."""
    _, version, repository = await _construct(make_fetcher, connection)

    for row in repository.members(version.id):
        assert row["listed_from"] is not None
        assert row["exchange"] in {"NYSE", "NASDAQ"}
        assert row["interval_evidence"].startswith("from=")
