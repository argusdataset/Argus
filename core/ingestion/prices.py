"""The daily full-universe OHLCV pull. Required, unconditional, no tiering.

This is the half of Module 26 with a deadline. Module 18's readiness
check counts how many universe members have a bar for the scan date, and
refuses to scan a day that is only partly delivered — so if this does not
finish, the scanner records `DATA_NOT_READY` and the day is lost.
Everything about the shape of this file follows from that.

## Nothing here fetches or normalizes

Module 04 fetches, Module 05 normalizes and persists. This decides *when*
and *for whom*, and calls them.

Concretely, the per-symbol path calls Module 04's
`backfill_daily_history`, which already owns the three things this pull
needs and would otherwise be rebuilt here: bounded concurrency against
the shared rate limiter, a per-symbol failure that is recorded rather
than fatal, and the JSONL checkpoint. Reusing it is not laziness — a
second concurrency-and-checkpoint implementation is a second one to get
wrong, and Module 04's is the one the historical backfill has already
run against ten thousand symbols.

## Two skips, doing different jobs

**The database skip.** Before fetching anything, ask which members
already hold a bar for the target date and drop them. This is what makes
a re-run of the same day cost nothing, and it has to be the durable
answer because a Railway cron container starts each firing with an empty
filesystem — the checkpoint file from the previous run is simply not
there.

**The checkpoint skip.** Module 04's checkpoint, within a run. A process
killed halfway through ten thousand symbols and restarted by the
platform in the same container resumes where it stopped instead of
starting over.

One caveat worth stating: the checkpoint records a symbol as complete
when its *fetch* succeeds, which is a moment before this module's
persist. A persist that then fails leaves the symbol checkpointed and
unwritten, and the in-run resume will skip it — but the next day's run
finds no bar for it in the database and fetches it again with a window
wide enough to cover the gap. The two skips cover each other's failure.

## The lookback window is what makes a missed day self-repairing

Each request asks for `incremental_lookback_days` of history ending at
the target session rather than that session alone. A security that got no
bar on Tuesday is fetched on Wednesday with a window covering both, so
the hole closes with no catch-up machinery. `persist()` is insert-only,
so the days that are already there are offered and declined.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine

from core.ingestion.config import IngestionConfig
from core.ingestion.members import MemberSet, securities_with_bars_on
from core.ingestion.strategy import EndpointStrategy, StrategyDecision
from data.normalization.persistence import CanonicalWriter
from data.normalization.pipeline import normalize_security, persist
from data.provider_adapters.fmp.models import DailyBar
from infra.observability.logging import get_logger

__all__ = ["OhlcvReport", "PriceSource", "ingest_daily_prices"]

_log = get_logger("argus.ingestion.prices")


class PriceSource(Protocol):
    """The slice of Module 04's `FmpFetcher` this path uses.

    A Protocol rather than the concrete class so a test can drive the
    whole pull without HTTP. `FmpFetcher` satisfies it structurally;
    nothing had to be added to Module 04 to make that true.
    """

    async def fetch_eod_for_date(self, as_of: date) -> Any: ...

    async def backfill_daily_history(
        self,
        symbols: Any,
        *,
        job_name: str = ...,
        start: date | None = ...,
        end: date | None = ...,
        on_records: Any = ...,
    ) -> Any: ...


@dataclass(slots=True)
class OhlcvReport:
    """What one day's price pull did, in numbers a log line can carry."""

    trading_date: date
    strategy: str
    universe_size: int = 0
    unresolved_tickers: int = 0
    already_held: int = 0
    attempted: int = 0
    already_checkpointed: int = 0
    bars_offered: int = 0
    bars_inserted: int = 0
    securities_written: int = 0
    empty: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    rejected: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date.isoformat(),
            "strategy": self.strategy,
            "universe_size": self.universe_size,
            "unresolved_tickers": self.unresolved_tickers,
            "already_held": self.already_held,
            "attempted": self.attempted,
            "already_checkpointed": self.already_checkpointed,
            "bars_offered": self.bars_offered,
            "bars_inserted": self.bars_inserted,
            "securities_written": self.securities_written,
            "empty": len(self.empty),
            "failed": len(self.failed),
            "rejected": self.rejected,
        }


async def ingest_daily_prices(
    engine: Engine,
    source: PriceSource,
    *,
    members: MemberSet,
    trading_date: date,
    decision: StrategyDecision,
    config: IngestionConfig | None = None,
) -> OhlcvReport:
    """Fetch, normalize and persist the target session for every member."""
    resolved = config or IngestionConfig()
    report = OhlcvReport(trading_date=trading_date, strategy=decision.strategy.value)
    report.universe_size = members.size
    report.unresolved_tickers = len(members.unresolved)

    if not members.members:
        return report

    with engine.begin() as connection:
        held = securities_with_bars_on(
            connection,
            [member.security_id for member in members.members],
            trading_date=trading_date,
        )

    outstanding = [member for member in members.members if member.security_id not in held]
    report.already_held = len(members.members) - len(outstanding)

    if not outstanding:
        _log.info(
            "every universe member already holds this session",
            extra={"event": "ohlcv_already_complete", **report.as_dict()},
        )
        return report

    identities = {member.ticker: member.security_id for member in outstanding}

    if decision.strategy is EndpointStrategy.BULK:
        await _ingest_bulk(engine, source, identities, trading_date, report, resolved)
    else:
        await _ingest_per_symbol(engine, source, identities, trading_date, report, resolved)

    _log.info(
        "daily price ingestion finished",
        extra={"event": "ohlcv_ingested", **report.as_dict()},
    )
    return report


async def _ingest_per_symbol(
    engine: Engine,
    source: PriceSource,
    identities: dict[str, UUID],
    trading_date: date,
    report: OhlcvReport,
    config: IngestionConfig,
) -> None:
    """One standard-tier request per outstanding symbol, via Module 04."""
    lookback = timedelta(days=config.settings.lookback_days)

    def on_records(result: Any) -> None:
        """Persist one symbol's bars. Never raises into Module 04's gather.

        `backfill_daily_history` calls this outside its own try/except, so
        an exception here would abort every remaining symbol. A failure
        to write one security must not cost the rest of the universe.
        """
        if not result.records:
            return
        symbol = result.records[0].symbol
        security_id = identities.get(symbol)
        if security_id is None:
            return
        try:
            _persist_bars(engine, security_id, list(result.records), report)
        except Exception as error:  # noqa: BLE001 - one security must not stop the run
            report.failed[symbol] = f"{type(error).__name__}: {error}"
            _log.warning(
                "persisting a symbol's bars failed",
                extra={
                    "event": "ohlcv_persist_failed",
                    "symbol": symbol,
                    "error_type": type(error).__name__,
                },
            )

    backfill = await source.backfill_daily_history(
        sorted(identities),
        job_name=_job_name(trading_date),
        start=trading_date - lookback,
        end=trading_date,
        on_records=on_records,
    )

    report.attempted = backfill.attempted
    report.already_checkpointed = backfill.already_complete
    report.empty = list(backfill.empty)
    report.failed.update(backfill.failed)


async def _ingest_bulk(
    engine: Engine,
    source: PriceSource,
    identities: dict[str, UUID],
    trading_date: date,
    report: OhlcvReport,
    config: IngestionConfig,
) -> None:
    """One bulk request for the whole session, grouped by symbol.

    No lookback window: the bulk endpoint answers for one date. That is
    the one real asymmetry between the two strategies — the bulk path
    does not self-repair a missed day, so a day missed under it stays
    missed until a per-symbol run or a backfill covers it. Recorded here
    rather than discovered later.
    """
    _ = config
    result = await source.fetch_eod_for_date(trading_date)

    grouped: dict[str, list[DailyBar]] = {}
    for bar in result.records:
        if bar.symbol in identities:
            grouped.setdefault(bar.symbol, []).append(bar)

    report.attempted = len(grouped)
    for symbol, bars in grouped.items():
        try:
            _persist_bars(engine, identities[symbol], bars, report)
        except Exception as error:  # noqa: BLE001 - one security must not stop the run
            report.failed[symbol] = f"{type(error).__name__}: {error}"

    report.empty = sorted(set(identities) - set(grouped))


def _persist_bars(
    engine: Engine,
    security_id: UUID,
    bars: list[DailyBar],
    report: OhlcvReport,
) -> None:
    """Normalize and write one security's bars in its own transaction.

    One transaction per security, deliberately: `CanonicalWriter` takes a
    connection rather than an engine so the caller chooses the boundary,
    and a single transaction spanning ten thousand securities would hold
    locks for the length of the whole run and lose everything to one
    failure at the end.
    """
    with engine.begin() as connection:
        outcome = normalize_security(security_id=security_id, bars=bars)
        persist(outcome, CanonicalWriter(connection))

    written = outcome.writes["bars"]
    report.bars_offered += written.offered
    report.bars_inserted += written.inserted
    report.rejected += len(outcome.translation.rejected) + len(outcome.validation.fatal)
    if written.inserted:
        report.securities_written += 1


def _job_name(trading_date: date) -> str:
    """Checkpoint file name, one per target session.

    Per date rather than one growing file: the units a run iterates over
    are that date's outstanding symbols, and a shared file would make
    `pending()` skip a symbol that completed for a different session.
    """
    return f"ingestion_ohlcv_{trading_date.isoformat()}"
