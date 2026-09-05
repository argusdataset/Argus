"""Fetching the Ultimate-plan data Module 19's Terminal serves.

The ingestion half of Vazifa 4: eight FMP endpoints, translated by
`data/normalization/terminal_records.py` and written to the three tables
`infra/db/schema/terminal_data.py` defines. Nothing here reads those rows
back — that is `services/terminal/`'s job, and keeping the two apart is
what lets the Terminal state honestly that it computes nothing.

## Tier-paced, not swept

Every one of these endpoints is per-symbol, so a nightly sweep of a
ten-thousand-name universe would cost ~70,000 requests. They are paced by
Module 26's own tier decision instead — `tiers.decide`, the same one
fundamentals and news use — so a name in DOWN_TREND is refreshed every
thirty days and one approaching a breakout every day.

The cost is stated rather than hidden: analyst coverage of a
long-forgotten name can be up to thirty days stale. That is the right
trade for data nobody is looking at, and the moment a security becomes
interesting enough to reach BREAKOUT_READY its cadence becomes daily.

## A separate step, not a change to `deep_refresh.py`

This could have been folded into Module 26's existing per-security loop.
It is not, deliberately: that loop is stable, deployed, and covered by
tests that assert exact request counts, and threading eight more fetches
through it would have put all of that at risk to save a file. The tier
decision it uses is imported rather than reimplemented, so the two
cadences cannot drift apart.

## The ETF gap, stated plainly

Two of the eight endpoints — ETF holdings and fund disclosures — only
mean anything for a fund, and **ARGUS cannot tell a fund from an
operating company**: `security_identity` stores `figi`, `cik` and `name`,
and nothing else. Module 04's `SecurityListing` does carry a
`security_type`, and Module 06 discards it when constructing the
universe.

So fund holdings are fetched only for symbols a caller names in
`fund_tickers`, which is empty by default. Asking every operating company
what it holds would be two wasted requests per security per refresh and a
slightly dishonest question. The fix is small and belongs to Module 06 —
persist `security_type` — and until it happens this parameter is the
honest interface.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine

from core.ingestion.config import IngestionConfig
from core.ingestion.members import UniverseMember
from data.normalization.terminal_records import (
    StoredDisclosure,
    StoredGrade,
    StoredIndicatorPoint,
    StoredSnapshot,
    TerminalTranslationError,
    translate_analyst_estimate,
    translate_executive_compensation,
    translate_fund_holdings,
    translate_grade,
    translate_peers,
    translate_price_target,
    translate_technical_indicator,
    translate_transcript,
    write_disclosures,
    write_grades,
    write_indicators,
    write_snapshots,
)
from data.provider_adapters.fmp.endpoints import TECHNICAL_INDICATORS
from data.provider_adapters.fmp.errors import FmpError
from infra.observability.logging import get_logger

__all__ = [
    "INDICATOR_PERIODS",
    "REQUIRED_METHODS",
    "TerminalDataReport",
    "TerminalDataSource",
    "ingest_terminal_data",
]

_log = get_logger("argus.ingestion.terminal_data")

#: Period length per indicator, for the daily series.
#:
#: One period each rather than the several a chartist would flip between:
#: nine indicators already cost nine requests per security per refresh,
#: and a second period length would double that for a series the Terminal
#: can only draw one of at a time. The values are the conventional
#: defaults — 50 bars for the moving averages, 14 for the oscillators —
#: and a caller wanting another passes `indicator_periods`.
INDICATOR_PERIODS: dict[str, int] = {
    "sma": 50,
    "ema": 50,
    "wma": 50,
    "dema": 50,
    "tema": 50,
    "rsi": 14,
    "standarddeviation": 14,
    "williams": 14,
    "adx": 14,
}

#: The timeframe the Terminal charts. FMP offers intraday too; a daily
#: series is what a base-formation view needs and what Module 19's own
#: datafeed serves.
INDICATOR_TIMEFRAME = "1day"

#: Every fetcher method this path needs. A provider missing any of them
#: is reported as skipped rather than crashing the run — see
#: `ingest_terminal_data`.
REQUIRED_METHODS: tuple[str, ...] = (
    "fetch_analyst_estimates",
    "fetch_price_target_consensus",
    "fetch_price_target_summary",
    "fetch_analyst_grades",
    "fetch_executive_compensation",
    "fetch_stock_peers",
    "fetch_earnings_transcript",
    "fetch_technical_indicator",
)


class TerminalDataSource(Protocol):
    """The slice of Module 04's fetcher this path uses."""

    async def fetch_analyst_estimates(
        self, symbol: str, *, period: str = ..., limit: int = ...
    ) -> Any: ...

    async def fetch_price_target_consensus(self, symbol: str) -> Any: ...

    async def fetch_price_target_summary(self, symbol: str) -> Any: ...

    async def fetch_analyst_grades(self, symbol: str, *, limit: int = ...) -> Any: ...

    async def fetch_executive_compensation(self, symbol: str) -> Any: ...

    async def fetch_stock_peers(self, symbol: str) -> Any: ...

    async def fetch_earnings_transcript(
        self, symbol: str, *, year: int | None = ..., quarter: int | None = ...
    ) -> Any: ...

    async def fetch_technical_indicator(
        self, symbol: str, indicator: str, *, period_length: int = ..., timeframe: str = ...
    ) -> Any: ...


@dataclass(slots=True)
class TerminalDataReport:
    """What one pass fetched and wrote, per table."""

    trading_date: date
    considered: int = 0
    disclosures_offered: int = 0
    disclosures_written: int = 0
    snapshots_offered: int = 0
    snapshots_written: int = 0
    grades_offered: int = 0
    grades_written: int = 0
    indicator_points_offered: int = 0
    indicator_points_written: int = 0
    requests: int = 0
    #: Records the provider returned that could not be translated without
    #: inventing part of their key. Counted rather than raised: one
    #: unusable estimate must not cost a security its transcript.
    rejected: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    skipped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date.isoformat(),
            "considered": self.considered,
            "disclosures_offered": self.disclosures_offered,
            "disclosures_written": self.disclosures_written,
            "snapshots_offered": self.snapshots_offered,
            "snapshots_written": self.snapshots_written,
            "grades_offered": self.grades_offered,
            "grades_written": self.grades_written,
            "indicator_points_offered": self.indicator_points_offered,
            "indicator_points_written": self.indicator_points_written,
            "requests": self.requests,
            "rejected": len(self.rejected),
            "failed": len(self.failed),
            "skipped_reason": self.skipped_reason,
        }


@dataclass(slots=True)
class _Translated:
    """One security's rows, before they reach a transaction."""

    disclosures: list[StoredDisclosure] = field(default_factory=list)
    snapshots: list[StoredSnapshot] = field(default_factory=list)
    grades: list[StoredGrade] = field(default_factory=list)
    indicators: list[StoredIndicatorPoint] = field(default_factory=list)


async def ingest_terminal_data(
    engine: Engine,
    source: TerminalDataSource,
    *,
    due: list[UniverseMember],
    trading_date: date,
    config: IngestionConfig | None = None,
    fund_tickers: frozenset[str] = frozenset(),
    indicator_periods: dict[str, int] | None = None,
    report: TerminalDataReport | None = None,
    max_concurrency: int = 1,
) -> TerminalDataReport:
    """Fetch and store the Terminal's Ultimate-plan data for due securities.

    A provider that cannot serve these endpoints is reported as skipped
    rather than crashing the run — the same arrangement
    `core/ingestion/ownership.py` uses, and reported for the same reason:
    a run whose analyst data never arrived should be visibly that, not
    indistinguishable from a run where nobody had coverage.
    """
    resolved = config or IngestionConfig()
    outcome = report or TerminalDataReport(trading_date=trading_date)
    outcome.considered = len(due)

    missing = [name for name in REQUIRED_METHODS if not hasattr(source, name)]
    if missing:
        outcome.skipped_reason = (
            f"This provider does not implement {', '.join(missing)}, so no analyst, "
            "governance or holdings data was fetched. The Terminal's new endpoints "
            "report it as unavailable until it does — see "
            "core/ingestion/terminal_data.py."
        )
        return outcome

    if not due:
        return outcome

    semaphore = asyncio.Semaphore(max(max_concurrency, 1))

    async def one(member: UniverseMember) -> None:
        async with semaphore:
            try:
                translated = await _fetch_one(
                    source,
                    member,
                    config=resolved,
                    outcome=outcome,
                    include_fund_holdings=member.ticker.upper() in fund_tickers,
                    indicator_periods=indicator_periods or INDICATOR_PERIODS,
                )
            except FmpError as error:
                outcome.failed[member.ticker] = f"{type(error).__name__}: {error}"
                return

        try:
            with engine.begin() as connection:
                disclosures = write_disclosures(connection, translated.disclosures)
                snapshots = write_snapshots(connection, translated.snapshots)
                grades = write_grades(connection, translated.grades)
                indicators = write_indicators(connection, translated.indicators)
        except Exception as error:  # noqa: BLE001 - one security must not stop the run
            outcome.failed[member.ticker] = f"{type(error).__name__}: {error}"
            _log.warning(
                "terminal data persist failed",
                extra={
                    "event": "terminal_data_persist_failed",
                    "ticker": member.ticker,
                    "error_type": type(error).__name__,
                },
            )
            return

        outcome.disclosures_offered += disclosures.offered
        outcome.disclosures_written += disclosures.inserted
        outcome.snapshots_offered += snapshots.offered
        outcome.snapshots_written += snapshots.inserted
        outcome.grades_offered += grades.offered
        outcome.grades_written += grades.inserted
        outcome.indicator_points_offered += indicators.offered
        outcome.indicator_points_written += indicators.inserted

    await asyncio.gather(*(one(member) for member in due))

    _log.info(
        "terminal data ingestion finished",
        extra={"event": "terminal_data_finished", **outcome.as_dict()},
    )
    return outcome


async def _fetch_one(
    source: TerminalDataSource,
    member: UniverseMember,
    *,
    config: IngestionConfig,
    outcome: TerminalDataReport,
    include_fund_holdings: bool,
    indicator_periods: dict[str, int],
) -> _Translated:
    """Every configured type for one symbol, sequentially.

    Sequential rather than gathered, the same reasoning `deep_refresh.py`
    gives: the concurrency ceiling belongs to the run as a whole, and
    fanning out inside one security as well would make the real ceiling
    the product of the two and quietly exceed the rate limiter's budget.
    """
    ticker = member.ticker
    security_id = member.security_id
    rows = _Translated()

    estimates = await source.fetch_analyst_estimates(
        ticker, period=config.statement_period, limit=int(config.settings.statements)
    )
    outcome.requests += 1
    _collect(
        rows.disclosures,
        estimates.records,
        translate_analyst_estimate,
        security_id,
        outcome,
        f"estimates:{ticker}",
    )

    for fetch in (source.fetch_price_target_consensus, source.fetch_price_target_summary):
        targets = await fetch(ticker)
        outcome.requests += 1
        _collect(
            rows.snapshots,
            targets.records,
            translate_price_target,
            security_id,
            outcome,
            f"price_target:{ticker}",
        )

    grades = await source.fetch_analyst_grades(ticker)
    outcome.requests += 1
    _collect(rows.grades, grades.records, translate_grade, security_id, outcome, f"grades:{ticker}")

    compensation = await source.fetch_executive_compensation(ticker)
    outcome.requests += 1
    _collect(
        rows.disclosures,
        compensation.records,
        translate_executive_compensation,
        security_id,
        outcome,
        f"compensation:{ticker}",
    )

    peers = await source.fetch_stock_peers(ticker)
    outcome.requests += 1
    _collect(
        rows.snapshots, peers.records, translate_peers, security_id, outcome, f"peers:{ticker}"
    )

    transcript = await source.fetch_earnings_transcript(ticker)
    outcome.requests += 1
    _collect(
        rows.disclosures,
        transcript.records,
        translate_transcript,
        security_id,
        outcome,
        f"transcript:{ticker}",
    )

    for indicator in TECHNICAL_INDICATORS:
        period = indicator_periods.get(indicator)
        if period is None:
            # Not in the configured set. Skipped rather than defaulted:
            # a period length nobody chose is a series nobody asked for.
            continue
        series = await source.fetch_technical_indicator(
            ticker, indicator, period_length=period, timeframe=INDICATOR_TIMEFRAME
        )
        outcome.requests += 1
        _collect(
            rows.indicators,
            series.records,
            translate_technical_indicator,
            security_id,
            outcome,
            f"{indicator}:{ticker}",
        )

    if include_fund_holdings:
        # Only for symbols the caller named as funds. See the module
        # docstring on why ARGUS cannot work this out for itself.
        for fetch_positions, label in (
            (getattr(source, "fetch_etf_holdings", None), "etf"),
            (getattr(source, "fetch_fund_disclosure", None), "fund"),
        ):
            if fetch_positions is None:
                continue
            positions = await fetch_positions(ticker)
            outcome.requests += 1
            if not positions.records:
                continue
            try:
                rows.snapshots.append(translate_fund_holdings(positions.records, security_id))
            except TerminalTranslationError as error:
                outcome.rejected[f"{label}_holdings:{ticker}"] = str(error)

    return rows


def _collect(
    target: list[Any],
    records: list[Any],
    translate: Any,
    security_id: UUID,
    outcome: TerminalDataReport,
    label: str,
) -> None:
    """Translate what can be translated; count the rest.

    A record missing the field its key needs is rejected and recorded
    rather than stored under a guess — `translate.py`'s rule — and one
    such record must not cost the security the other seven types.
    """
    for record in records:
        try:
            target.append(translate(record, security_id))
        except TerminalTranslationError as error:
            outcome.rejected[label] = str(error)
