"""Fetching and storing 8-K, Form 4 and 13F — Module 26's Ultimate half.

Two things are under test. The ordinary path: a provider that serves
these endpoints has its rows translated and written, with unmatched
symbols dropped rather than attached to an invented identity. And the
path that matters more today: a provider that does **not** serve them —
which is every `FmpFetcher` built before the Ultimate plan, and every
test double written before this existed — is reported as skipped rather
than crashing a run that was otherwise fine.

The provider here is fake for the reason Module 26's own suite gives:
whether the fetchers work is Module 04's business and Module 04 tests it
against recorded payloads. What is under test here is what this module
does with what comes back.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Engine, func, select

from core.ingestion.members import MemberSet, UniverseMember
from core.ingestion.ownership import ingest_filings, ingest_ownership
from data.provider_adapters.fmp.models import (
    FetchProvenance,
    InsiderTransaction,
    InstitutionalOwnershipSummary,
    SecFiling,
)
from infra.db.schema.ownership_signals import insider_trades, institutional_ownership
from infra.db.schema.sec_filings import sec_filings

TRADING_DATE = date(2026, 3, 9)
FETCHED_AT = datetime(2026, 3, 10, 21, 0, tzinfo=UTC)


def _provenance(endpoint: str) -> FetchProvenance:
    return FetchProvenance(endpoint=endpoint, url_path=f"/stable/{endpoint}", fetched_at=FETCHED_AT)


class _Result:
    def __init__(self, records: list[Any]) -> None:
        self.records = records


class FakeUltimateFetcher:
    """A provider that serves the three Ultimate endpoints, and counts calls."""

    def __init__(self, *, filings=None, trades=None, holdings=None) -> None:
        self._filings = filings or []
        self._trades = trades or {}
        self._holdings = holdings or {}
        self.filing_pages: list[int] = []
        self.trade_requests: list[str] = []
        self.holding_requests: list[str] = []

    async def fetch_latest_8k_filings(self, *, page: int = 0, limit: int = 100):
        self.filing_pages.append(page)
        return _Result(self._filings if page == 0 else [])

    async def fetch_insider_trades(self, symbol: str, *, page: int = 0, limit: int = 100):
        self.trade_requests.append(symbol)
        return _Result(self._trades.get(symbol, []))

    async def fetch_institutional_ownership(
        self, symbol: str, *, year: int | None = None, quarter: int | None = None
    ):
        self.holding_requests.append(symbol)
        return _Result(self._holdings.get(symbol, []))


class LegacyFetcher:
    """A provider from before the Ultimate plan. Serves none of them."""


def _members(identities: dict[str, Any]) -> MemberSet:
    return MemberSet(
        members=tuple(
            UniverseMember(security_id=security_id, ticker=ticker)
            for ticker, security_id in identities.items()
        ),
        unresolved=(),
    )


def _count(engine: Engine, table, security_id) -> int:
    with engine.connect() as connection:
        return connection.execute(
            select(func.count()).select_from(table).where(table.c.security_id == security_id)
        ).scalar_one()


# --------------------------------------------------------------------------
# 8-K: one bulk request for the whole market
# --------------------------------------------------------------------------


def test_filings_for_universe_members_are_stored(engine, universe):
    _version_id, identities = universe(("AAA",))
    ticker = next(iter(identities))
    security_id = identities[ticker]
    members = _members({ticker: security_id})

    source = FakeUltimateFetcher(
        filings=[
            SecFiling(
                provenance=_provenance("sec_8k_latest"),
                symbol=ticker,
                form_type="8-K",
                raw={
                    "symbol": ticker,
                    "acceptedDate": "2026-03-09 16:31:00",
                    "finalLink": "https://sec.gov/a.htm",
                    "description": "Item 5.02 Departure of Directors",
                },
            )
        ]
    )

    report = asyncio.run(ingest_filings(engine, source, members=members, trading_date=TRADING_DATE))

    assert report.filings_written == 1
    assert report.filings_unmatched == 0
    assert _count(engine, sec_filings, security_id) == 1

    with engine.connect() as connection:
        row = connection.execute(
            select(sec_filings).where(sec_filings.c.security_id == security_id)
        ).one()
    assert row.item_numbers == ["5.02"]
    assert row.link == "https://sec.gov/a.htm"
    # The alias that actually resolved is recorded — the whole point of
    # the FIELD_ALIASES table while FMP's field names are unconfirmed.
    assert row.lineage["resolved_fields"]["filed_at"] == "acceptedDate"


def test_a_filer_outside_the_universe_is_dropped_not_stored(engine, universe):
    """An unknown ticker has no `security_id` to attach to, and inventing
    one would be worse than skipping it."""
    _version_id, identities = universe(("BBB",))
    ticker = next(iter(identities))
    members = _members({ticker: identities[ticker]})

    source = FakeUltimateFetcher(
        filings=[
            SecFiling(
                provenance=_provenance("sec_8k_latest"),
                symbol="NOTOURS",
                form_type="8-K",
                raw={"symbol": "NOTOURS", "acceptedDate": "2026-03-09 16:31:00"},
            )
        ]
    )

    report = asyncio.run(ingest_filings(engine, source, members=members, trading_date=TRADING_DATE))

    assert report.filings_written == 0
    assert report.filings_unmatched == 1


def test_re_ingesting_the_same_filing_writes_nothing_new(engine, universe):
    """Insert-only: a bulk feed repeats yesterday's filings on every page,
    and a second run must not double them."""
    _version_id, identities = universe(("CCC",))
    ticker = next(iter(identities))
    security_id = identities[ticker]
    members = _members({ticker: security_id})

    filing = SecFiling(
        provenance=_provenance("sec_8k_latest"),
        symbol=ticker,
        form_type="8-K",
        raw={
            "symbol": ticker,
            "acceptedDate": "2026-03-09 16:31:00",
            "finalLink": "https://sec.gov/b.htm",
        },
    )

    first = asyncio.run(
        ingest_filings(
            engine,
            FakeUltimateFetcher(filings=[filing]),
            members=members,
            trading_date=TRADING_DATE,
        )
    )
    second = asyncio.run(
        ingest_filings(
            engine,
            FakeUltimateFetcher(filings=[filing]),
            members=members,
            trading_date=TRADING_DATE,
        )
    )

    assert first.filings_written == 1
    assert second.filings_written == 0
    assert _count(engine, sec_filings, security_id) == 1


def test_a_filing_with_no_readable_date_is_rejected_and_reported(engine, universe):
    """Rejected rather than dated by guess, and counted so the run says so."""
    _version_id, identities = universe(("DDD",))
    ticker = next(iter(identities))
    members = _members({ticker: identities[ticker]})

    source = FakeUltimateFetcher(
        filings=[
            SecFiling(
                provenance=_provenance("sec_8k_latest"),
                symbol=ticker,
                form_type="8-K",
                raw={"symbol": ticker, "finalLink": "https://sec.gov/c.htm"},
            )
        ]
    )

    report = asyncio.run(ingest_filings(engine, source, members=members, trading_date=TRADING_DATE))

    assert report.filings_written == 0
    assert report.rejected


# --------------------------------------------------------------------------
# Form 4 and 13F: per-symbol, for the securities that are due
# --------------------------------------------------------------------------


def test_insider_trades_and_holdings_are_stored_for_a_due_security(engine, universe):
    _version_id, identities = universe(("EEE",))
    ticker = next(iter(identities))
    security_id = identities[ticker]
    member = UniverseMember(security_id=security_id, ticker=ticker)

    source = FakeUltimateFetcher(
        trades={
            ticker: [
                InsiderTransaction(
                    provenance=_provenance("insider_trading_search"),
                    symbol=ticker,
                    raw={
                        "transactionDate": "2026-03-02",
                        "transactionType": "P-Purchase",
                        "reportingName": "Jane Doe",
                        "typeOfOwner": "officer: CFO",
                        "securitiesTransacted": "5000",
                        "price": "12.5",
                    },
                )
            ]
        },
        holdings={
            ticker: [
                InstitutionalOwnershipSummary(
                    provenance=_provenance("institutional_ownership_summary"),
                    symbol=ticker,
                    year=2025,
                    quarter=4,
                    raw={"investorsHolding": 310, "numberOf13Fshares": 1_200_000},
                )
            ]
        },
    )

    report = asyncio.run(ingest_ownership(engine, source, due=[member], trading_date=TRADING_DATE))

    assert report.insider_written == 1
    assert report.institutional_written == 1
    assert source.trade_requests == [ticker]
    assert source.holding_requests == [ticker]

    with engine.connect() as connection:
        trade = connection.execute(
            select(insider_trades).where(insider_trades.c.security_id == security_id)
        ).one()
    assert trade.transaction_code == "P"
    assert trade.reporting_person == "Jane Doe"
    # Traded 2 March, knowable two days later — never on the trade date.
    assert trade.event_time.date() == date(2026, 3, 2)
    assert trade.availability_time > trade.event_time


def test_a_security_that_is_not_due_is_never_requested(engine, universe):
    """The tier decision is the caller's; this proves the fetch respects
    it rather than sweeping the whole universe every night."""
    _version_id, identities = universe(("FFF",))
    source = FakeUltimateFetcher()

    report = asyncio.run(ingest_ownership(engine, source, due=[], trading_date=TRADING_DATE))

    assert source.trade_requests == []
    assert source.holding_requests == []
    assert report.requests == 0
    assert report.considered == 0


def test_re_ingesting_the_same_quarter_and_trade_writes_nothing_new(engine, universe):
    _version_id, identities = universe(("GGG",))
    ticker = next(iter(identities))
    security_id = identities[ticker]
    member = UniverseMember(security_id=security_id, ticker=ticker)

    def _source() -> FakeUltimateFetcher:
        return FakeUltimateFetcher(
            trades={
                ticker: [
                    InsiderTransaction(
                        provenance=_provenance("insider_trading_search"),
                        symbol=ticker,
                        raw={
                            "transactionDate": "2026-03-02",
                            "transactionType": "P-Purchase",
                            "reportingName": "Jane Doe",
                            "securitiesTransacted": "5000",
                        },
                    )
                ]
            },
            holdings={
                ticker: [
                    InstitutionalOwnershipSummary(
                        provenance=_provenance("institutional_ownership_summary"),
                        symbol=ticker,
                        year=2025,
                        quarter=4,
                        raw={"investorsHolding": 310},
                    )
                ]
            },
        )

    first = asyncio.run(
        ingest_ownership(engine, _source(), due=[member], trading_date=TRADING_DATE)
    )
    second = asyncio.run(
        ingest_ownership(engine, _source(), due=[member], trading_date=TRADING_DATE)
    )

    assert (first.insider_written, first.institutional_written) == (1, 1)
    assert (second.insider_written, second.institutional_written) == (0, 0)
    assert _count(engine, insider_trades, security_id) == 1
    assert _count(engine, institutional_ownership, security_id) == 1


# --------------------------------------------------------------------------
# A provider that cannot serve these at all
# --------------------------------------------------------------------------


def test_a_legacy_provider_is_reported_as_skipped_rather_than_crashing(engine, universe):
    """Reported, not silently ignored: a run whose filings never arrived
    should be visibly that, because the counters would otherwise read
    identically to a genuinely quiet day."""
    _version_id, identities = universe(("HHH",))
    ticker = next(iter(identities))
    members = _members({ticker: identities[ticker]})
    member = UniverseMember(security_id=identities[ticker], ticker=ticker)

    filings = asyncio.run(
        ingest_filings(engine, LegacyFetcher(), members=members, trading_date=TRADING_DATE)
    )
    ownership = asyncio.run(
        ingest_ownership(engine, LegacyFetcher(), due=[member], trading_date=TRADING_DATE)
    )

    assert filings.skipped_reason and "fetch_latest_8k_filings" in filings.skipped_reason
    assert ownership.skipped_reason and "fetch_insider_trades" in ownership.skipped_reason
    assert filings.filings_written == 0
    assert ownership.insider_written == 0
