"""Ingesting the Terminal's Ultimate-plan data, end to end.

A fake provider rather than a live key: FMP's Ultimate plan was not
purchased when this was written, so the exercise is the code path, not
the endpoint. What that path has to get right is narrow and worth naming:

1. **A provider that cannot serve these endpoints is skipped, not fatal.**
   ARGUS's current key has no Ultimate access, so this is not a
   hypothetical — it is the state the system is in today, and a run that
   crashed on it would take the rest of the ingestion down with it.
2. **One unusable record does not cost a security its other data.** An
   estimate with no period is rejected and counted; the transcript in the
   same pass still lands.
3. **Re-ingestion writes nothing.** The tables are append-only, so the
   writers are `ON CONFLICT DO NOTHING` and a second identical pass must
   insert zero rows rather than fail.

The third is the one that would go wrong silently. `DO UPDATE` would fire
migration 0003's guard and raise; a missing constraint would insert
duplicates nobody notices until a Terminal panel shows the same period
twice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, func, select

from core.ingestion.members import UniverseMember
from core.ingestion.terminal_data import (
    INDICATOR_PERIODS,
    REQUIRED_METHODS,
    ingest_terminal_data,
)
from data.provider_adapters.fmp.models import (
    AnalystEstimate,
    AnalystGrade,
    EarningsTranscript,
    ExecutiveCompensation,
    FetchProvenance,
    FundHolding,
    PriceTarget,
    SecurityPeerGroup,
    TechnicalIndicatorPoint,
)
from infra.db.schema.terminal_data import (
    analyst_grades,
    canonical_disclosures,
    canonical_snapshots,
    technical_indicators,
)

FETCHED_AT = datetime(2026, 3, 3, 21, 0, tzinfo=UTC)
TRADING_DATE = date(2026, 3, 3)


def _provenance(endpoint: str) -> FetchProvenance:
    return FetchProvenance(endpoint=endpoint, url_path=f"/stable/{endpoint}", fetched_at=FETCHED_AT)


@dataclass(slots=True)
class _Response:
    """The shape every fetcher returns: `.records`, and nothing else used."""

    records: list[Any] = field(default_factory=list)


class FakeUltimateSource:
    """A provider with the Ultimate endpoints, returning fixed payloads.

    Every method is present, so `REQUIRED_METHODS` is satisfied and the
    run proceeds. `broken_estimate` swaps in an estimate with no fiscal
    period, which is the one thing the translator refuses.
    """

    def __init__(self, *, broken_estimate: bool = False) -> None:
        self.broken_estimate = broken_estimate
        self.calls: list[str] = []

    async def fetch_analyst_estimates(
        self, symbol: str, *, period: str = "annual", limit: int = 10
    ) -> _Response:
        self.calls.append("estimates")
        raw: dict[str, Any] = (
            {"estimatedEpsAvg": 4.2}
            if self.broken_estimate
            else {"date": "2027-12-31", "estimatedEpsAvg": 4.2}
        )
        return _Response([AnalystEstimate(symbol=symbol, provenance=_provenance("est"), raw=raw)])

    async def fetch_price_target_consensus(self, symbol: str) -> _Response:
        self.calls.append("target_consensus")
        return _Response(
            [
                PriceTarget(
                    symbol=symbol,
                    source="consensus",
                    provenance=_provenance("ptc"),
                    raw={"targetConsensus": 210.0},
                )
            ]
        )

    async def fetch_price_target_summary(self, symbol: str) -> _Response:
        self.calls.append("target_summary")
        return _Response(
            [
                PriceTarget(
                    symbol=symbol,
                    source="summary",
                    provenance=_provenance("pts"),
                    raw={"lastMonthCount": 7},
                )
            ]
        )

    async def fetch_analyst_grades(self, symbol: str, *, limit: int = 100) -> _Response:
        self.calls.append("grades")
        return _Response(
            [
                AnalystGrade(
                    symbol=symbol,
                    provenance=_provenance("grades"),
                    raw={
                        "gradingCompany": "Alpha Bank",
                        "date": "2026-03-02",
                        "action": "upgrade",
                        "newGrade": "Buy",
                    },
                )
            ]
        )

    async def fetch_executive_compensation(self, symbol: str) -> _Response:
        self.calls.append("compensation")
        return _Response(
            [
                ExecutiveCompensation(
                    symbol=symbol,
                    provenance=_provenance("comp"),
                    raw={
                        "year": 2025,
                        "filingDate": "2026-02-20",
                        "nameAndPosition": "Jane Roe, CEO",
                        "total": 9_000_000,
                    },
                )
            ]
        )

    async def fetch_stock_peers(self, symbol: str) -> _Response:
        self.calls.append("peers")
        return _Response(
            [
                SecurityPeerGroup(
                    symbol=symbol, provenance=_provenance("peers"), raw={"peers": ["AAA", "BBB"]}
                )
            ]
        )

    async def fetch_earnings_transcript(
        self, symbol: str, *, year: int | None = None, quarter: int | None = None
    ) -> _Response:
        self.calls.append("transcript")
        return _Response(
            [
                EarningsTranscript(
                    symbol=symbol,
                    provenance=_provenance("transcript"),
                    year=2026,
                    quarter=1,
                    raw={"date": "2026-02-04 17:00:00", "content": "Operator: good afternoon."},
                )
            ]
        )

    async def fetch_technical_indicator(
        self, symbol: str, indicator: str, *, period_length: int = 14, timeframe: str = "1day"
    ) -> _Response:
        self.calls.append(f"indicator:{indicator}")
        return _Response(
            [
                TechnicalIndicatorPoint(
                    symbol=symbol,
                    indicator=indicator,
                    period_length=period_length,
                    timeframe=timeframe,
                    provenance=_provenance("indicator"),
                    raw={"date": "2026-03-02", indicator: "62.5", "value": "62.5"},
                )
            ]
        )

    async def fetch_etf_holdings(self, symbol: str) -> _Response:
        self.calls.append("etf_holdings")
        return _Response(
            [
                FundHolding(
                    symbol=symbol,
                    source="etf_holdings",
                    provenance=_provenance("etf"),
                    raw={"asset": "AAA", "weightPercentage": 7.1},
                )
            ]
        )


class ProviderWithoutUltimate:
    """Today's ARGUS: a fetcher with none of the new methods."""


def _member(security_id: UUID, ticker: str) -> UniverseMember:
    return UniverseMember(security_id=security_id, ticker=ticker)


def _count(engine: Engine, table: Any, security_id: UUID) -> int:
    """Scoped to one security. The database is shared across tests in this
    session and these tables are append-only, so a global count would be a
    running total of everything the suite ever wrote."""
    with engine.connect() as connection:
        return connection.execute(
            select(func.count()).select_from(table).where(table.c.security_id == security_id)
        ).scalar_one()


def _one(universe, label: str) -> tuple[str, UUID]:
    """One registered security, with the suffixed ticker it really has."""
    _version_id, identities = universe((label,))
    ticker = next(iter(identities))
    return ticker, identities[ticker]


def test_a_provider_without_the_endpoints_is_skipped_not_fatal(engine, universe) -> None:
    """The state ARGUS is actually in, and it must not crash a run.

    `skipped_reason` names every missing method rather than saying
    "unsupported": an operator reading a report should be able to see
    which half of the plan is missing without reading this file.
    """
    ticker, security_id = _one(universe, "TDA")

    report = asyncio.run(
        ingest_terminal_data(
            engine,
            ProviderWithoutUltimate(),  # type: ignore[arg-type]
            due=[_member(security_id, ticker)],
            trading_date=TRADING_DATE,
        )
    )

    assert report.skipped_reason is not None
    for method in REQUIRED_METHODS:
        assert method in report.skipped_reason
    assert report.disclosures_written == 0
    assert report.requests == 0


def test_one_pass_writes_every_type(engine, universe) -> None:
    """All four tables, from one security, in one run."""
    ticker, security_id = _one(universe, "TDB")

    report = asyncio.run(
        ingest_terminal_data(
            engine,
            FakeUltimateSource(),  # type: ignore[arg-type]
            due=[_member(security_id, ticker)],
            trading_date=TRADING_DATE,
            fund_tickers=frozenset({ticker.upper()}),
        )
    )

    assert report.failed == {}
    assert report.rejected == {}
    # Estimate, compensation, transcript.
    assert _count(engine, canonical_disclosures, security_id) == 3
    # Two price-target halves, peers, fund holdings.
    assert _count(engine, canonical_snapshots, security_id) == 4
    assert _count(engine, analyst_grades, security_id) == 1
    assert _count(engine, technical_indicators, security_id) == len(INDICATOR_PERIODS)


def test_both_price_target_halves_survive_one_pass(engine, universe) -> None:
    """The collision the two snapshot types exist to prevent.

    Both halves are fetched in the same run and both fake responses carry
    the same `fetched_at`, so under a single `PRICE_TARGET` type they
    would share the snapshot key exactly and the second would be dropped.
    Two types means two rows.
    """
    ticker, security_id = _one(universe, "TDC")

    asyncio.run(
        ingest_terminal_data(
            engine,
            FakeUltimateSource(),  # type: ignore[arg-type]
            due=[_member(security_id, ticker)],
            trading_date=TRADING_DATE,
        )
    )

    with engine.connect() as connection:
        types = (
            connection.execute(
                select(canonical_snapshots.c.snapshot_type).where(
                    canonical_snapshots.c.security_id == security_id
                )
            )
            .scalars()
            .all()
        )

    assert "PRICE_TARGET_CONSENSUS" in types
    assert "PRICE_TARGET_SUMMARY" in types


def test_a_second_identical_pass_writes_nothing(engine, universe) -> None:
    """Append-only means re-ingestion is a no-op, not a failure.

    `ON CONFLICT DO NOTHING` rather than `DO UPDATE`, because the tables
    carry migration 0003's guard and an UPDATE would be refused by the
    database. The constraint and the trigger have to agree, and this is
    what proves they do.
    """
    ticker, security_id = _one(universe, "TDD")
    source = FakeUltimateSource()
    due = [_member(security_id, ticker)]

    first = asyncio.run(
        ingest_terminal_data(engine, source, due=due, trading_date=TRADING_DATE)  # type: ignore[arg-type]
    )
    after_first = _count(engine, canonical_disclosures, security_id)

    second = asyncio.run(
        ingest_terminal_data(engine, source, due=due, trading_date=TRADING_DATE)  # type: ignore[arg-type]
    )

    assert first.disclosures_written > 0
    assert second.disclosures_written == 0
    assert second.failed == {}
    assert _count(engine, canonical_disclosures, security_id) == after_first


def test_one_unusable_record_does_not_cost_the_rest(engine, universe) -> None:
    """A rejected estimate is counted; the transcript still lands.

    Counted rather than raised, the rule `_collect` states: one estimate
    the translator cannot key must not take a security's whole pass with
    it.
    """
    ticker, security_id = _one(universe, "TDE")

    report = asyncio.run(
        ingest_terminal_data(
            engine,
            FakeUltimateSource(broken_estimate=True),  # type: ignore[arg-type]
            due=[_member(security_id, ticker)],
            trading_date=TRADING_DATE,
        )
    )

    assert len(report.rejected) == 1
    assert f"estimates:{ticker}" in report.rejected
    assert report.failed == {}
    # Compensation and transcript, but not the estimate.
    assert report.disclosures_written == 2


def test_holdings_are_fetched_only_for_named_funds(engine, universe) -> None:
    """The gap ARGUS cannot close on its own, made visible.

    No security-type flag is stored, so a fund is one the caller names.
    An operating company must not cost a holdings request per refresh —
    and the response for it correctly reports nothing.
    """
    ticker, security_id = _one(universe, "TDF")
    source = FakeUltimateSource()

    asyncio.run(
        ingest_terminal_data(
            engine,
            source,  # type: ignore[arg-type]
            due=[_member(security_id, ticker)],
            trading_date=TRADING_DATE,
        )
    )

    assert "etf_holdings" not in source.calls
