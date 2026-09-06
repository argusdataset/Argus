"""One-off historical price and corporate-action backfill.

## The twelve-month wait this exists to remove

`backfill_daily_history` accepts any `start`/`end` range, has a
checkpoint, bounded concurrency and per-symbol failure isolation — and
its only production caller is `core/ingestion/prices.py`, which always
asks for `trading_date - 7 days`. So history accumulates one day per day,
forwards from whenever ingestion first ran.

Set that against what ARGUS needs to produce anything:

- `core/feature_engine/spec.py`: `percentile: int = 252`.
- `consolidation.py` takes `atr_percentile` over that 252-bar window, and
  `rolling_percentile_rank` returns an all-NaN column below it.
- `core/market_state/states.py`'s CONSOLIDATION and ACCUMULATION
  predicates both require `atr_percentile`; a NaN predicate is never true.
- `core/lifecycle/engine.py`: `DETECTION_STATES` is those two states, and
  a setup opens from nothing else.

So without a backfill, **no setup can open for roughly 252 trading days**
— twelve to thirteen months. Meanwhile DOWN_TREND, BREAKOUT_READY and
UPTREND fill from about two months in and Telegram alerts start going
out, so the system looks alive while the outcome record, which is the
thing worth having, stays empty for a year.

## Corporate actions come with it, and that is not optional

Fifteen years of unadjusted prices is G2's bug with a longer reach: every
split in that history is an uncorrected discontinuity, and Module 15
reads each one as a catastrophic single-bar failure. `normalize_security`
takes bars and actions together and `persist` writes the actions first,
so fetching both here costs two extra requests per symbol and removes
that entire class of wrong outcome. Skipping them is possible
(`ARGUS_BACKFILL_ACTIONS=0`) and is the wrong default, so it is not the
default.

## Resumable, because it will be interrupted

A Railway container running fifteen years across ten thousand symbols
will be restarted at some point. `JobCheckpoint` records each symbol as
it completes and `pending()` skips those on the next run, so a restart
resumes. The job name carries the range, so backfilling 2010-2015 and
then 2015-2020 are separate jobs rather than the second one believing the
first had finished its work.

## It says what it will cost before it starts

Two requests per symbol for actions plus one for prices, against the
configured rate. An operator about to spend a day of an API quota should
see the number first, and see it in the log rather than having to work it
out from the universe size.

## Configuration, not arguments

`refuse_arguments` is why: a start command is a string in a web form, and
a `&&` in it silently became argv once already. So the range comes from
the environment:

    ARGUS_BACKFILL_START=2010-01-01      # required
    ARGUS_BACKFILL_END=2026-01-01        # optional, defaults to today
    ARGUS_BACKFILL_SYMBOLS=AAPL,MSFT     # optional, defaults to the universe
    ARGUS_BACKFILL_ACTIONS=0             # optional, off switch, on by default

There is no default start date. Fifteen years hardcoded would be a
spending decision this file is not entitled to make.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine

from core.ingestion.members import universe_members
from data.normalization.persistence import CanonicalWriter
from data.normalization.pipeline import normalize_security, persist
from data.provider_adapters.fmp.client import FmpClient
from data.provider_adapters.fmp.errors import FmpError
from data.provider_adapters.fmp.fetchers import FmpFetcher
from infra.db.connection import create_db_engine
from infra.deploy.cli import refuse_arguments
from infra.deploy.config import DeploymentProfile, profile_for
from infra.deploy.scanner import ScannerNotReady, ScannerSettings, resolve_universe_version
from infra.observability.logging import configure_logging, get_logger
from packages.config.settings import AppConfig, get_config

__all__ = ["BackfillReport", "BackfillSettings", "main", "run_backfill"]

_log = get_logger("argus.deploy.backfill")

#: Requests per symbol: one for the price history, one each for splits and
#: dividends. Named so the cost estimate below cannot drift from what the
#: run actually does.
REQUESTS_PER_SYMBOL_WITH_ACTIONS = 3
REQUESTS_PER_SYMBOL_PRICES_ONLY = 1


class BackfillNotConfigured(RuntimeError):
    """The range was not given, and there is no honest default for it."""


@dataclass(frozen=True, slots=True)
class BackfillSettings:
    """What to fetch. Read from the environment — see the module docstring."""

    start: date
    end: date
    #: Explicit symbols, or empty for "every universe member".
    symbols: tuple[str, ...] = ()
    include_actions: bool = True

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> BackfillSettings:
        source = env if env is not None else dict(os.environ)

        raw_start = source.get("ARGUS_BACKFILL_START", "").strip()
        if not raw_start:
            raise BackfillNotConfigured(
                "ARGUS_BACKFILL_START is not set. This job has no default range: "
                "how far back to fetch is a spending decision — at one request per "
                "symbol it is one pass over the universe whatever the range, but the "
                "range is what decides whether the result can produce a setup. "
                "Set it to an ISO date, e.g. ARGUS_BACKFILL_START=2010-01-01."
            )

        start = _iso_date("ARGUS_BACKFILL_START", raw_start)
        raw_end = source.get("ARGUS_BACKFILL_END", "").strip()
        end = _iso_date("ARGUS_BACKFILL_END", raw_end) if raw_end else datetime.now(UTC).date()

        if end < start:
            raise BackfillNotConfigured(
                f"ARGUS_BACKFILL_END ({end.isoformat()}) is before ARGUS_BACKFILL_START "
                f"({start.isoformat()}). An empty range would fetch nothing and report "
                "success, which is the failure this refuses to perform."
            )

        symbols = tuple(
            part.strip().upper()
            for part in source.get("ARGUS_BACKFILL_SYMBOLS", "").split(",")
            if part.strip()
        )
        return cls(
            start=start,
            end=end,
            symbols=symbols,
            include_actions=source.get("ARGUS_BACKFILL_ACTIONS", "1").strip() not in {"0", "false"},
        )

    def job_name(self) -> str:
        """Checkpoint file, one per range.

        The range is in the name so that backfilling 2010-2015 and then
        2015-2020 are two jobs. Sharing a checkpoint would make the
        second run believe the first had already covered its symbols and
        skip every one of them — a silent no-op that looks like a fast
        success.
        """
        scope = "universe" if not self.symbols else f"{len(self.symbols)}symbols"
        return f"backfill_{self.start.isoformat()}_{self.end.isoformat()}_{scope}"


@dataclass(slots=True)
class BackfillReport:
    """What one backfill fetched and wrote."""

    start: date
    end: date
    securities: int = 0
    estimated_requests: int = 0
    requests: int = 0
    bars_offered: int = 0
    bars_inserted: int = 0
    actions_offered: int = 0
    actions_inserted: int = 0
    securities_written: int = 0
    already_checkpointed: int = 0
    empty: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        """Whether the run is worth calling a success.

        Three cases, and the third is why this is not simply
        `bars_inserted > 0`:

        - Wrote bars: healthy, even if some symbols failed. Per-symbol
          isolation is the design and the checkpoint retries them.
        - Wrote nothing because every symbol was already checkpointed:
          healthy. That is what a resumed, completed job looks like.
        - No securities at all: **not** healthy, whatever the counters
          say. `already_checkpointed == securities` is trivially true at
          zero, and a job that had nothing to do because the universe
          resolved to nobody is a configuration problem — reporting it as
          a fast success is exactly the silence these entrypoints exist
          to avoid.
        """
        if not self.securities:
            return False
        return self.bars_inserted > 0 or self.already_checkpointed == self.securities

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "securities": self.securities,
            "estimated_requests": self.estimated_requests,
            "requests": self.requests,
            "bars_offered": self.bars_offered,
            "bars_inserted": self.bars_inserted,
            "actions_offered": self.actions_offered,
            "actions_inserted": self.actions_inserted,
            "securities_written": self.securities_written,
            "already_checkpointed": self.already_checkpointed,
            "empty": len(self.empty),
            "failed": len(self.failed),
        }


async def run_backfill(
    engine: Engine,
    source: Any,
    *,
    settings: BackfillSettings,
    identities: dict[str, UUID],
    app_config: AppConfig | None = None,
) -> BackfillReport:
    """Fetch and persist history for `identities`, resumably.

    `source` and `identities` are arguments rather than being built here
    so this is testable without a live key and without a universe — the
    same shape `run_daily_ingestion` uses and for the same reason.
    """
    report = BackfillReport(start=settings.start, end=settings.end)
    report.securities = len(identities)
    per_symbol = (
        REQUESTS_PER_SYMBOL_WITH_ACTIONS
        if settings.include_actions
        else REQUESTS_PER_SYMBOL_PRICES_ONLY
    )
    report.estimated_requests = len(identities) * per_symbol

    config = app_config or get_config()
    rate = max(config.providers.fmp_requests_per_minute, 1)
    _log.info(
        "backfill starting",
        extra={
            "event": "backfill_starting",
            "requests_per_symbol": per_symbol,
            "requests_per_minute": rate,
            # The number an operator wants before committing a day of
            # quota, in the unit they think in.
            "estimated_minutes": round(report.estimated_requests / rate, 1),
            "include_actions": settings.include_actions,
            "job_name": settings.job_name(),
            **report.as_dict(),
        },
    )

    if not identities:
        return report

    def on_records(result: Any) -> None:
        """Persist one symbol as it arrives. Never raises into the gather.

        `backfill_daily_history` calls this outside its own try/except,
        so an exception here would abort every remaining symbol — the
        same trap `core/ingestion/prices.py` documents at its own
        `on_records`.
        """
        if not result.records:
            return
        symbol = result.records[0].symbol
        security_id = identities.get(symbol)
        if security_id is None:
            return
        try:
            _persist(engine, security_id, list(result.records), [], report)
        except Exception as error:  # noqa: BLE001 - one security must not stop the run
            report.failed[symbol] = f"{type(error).__name__}: {error}"
            _log.warning(
                "persisting a symbol's history failed",
                extra={
                    "event": "backfill_persist_failed",
                    "symbol": symbol,
                    "error_type": type(error).__name__,
                },
            )

    outcome = await source.backfill_daily_history(
        sorted(identities),
        job_name=settings.job_name(),
        start=settings.start,
        end=settings.end,
        on_records=on_records,
    )
    report.requests += outcome.attempted
    report.already_checkpointed = outcome.already_complete
    report.empty = list(outcome.empty)
    report.failed.update(outcome.failed)

    if settings.include_actions:
        await _backfill_actions(engine, source, identities, report)

    _log.info("backfill finished", extra={"event": "backfill_finished", **report.as_dict()})
    return report


async def _backfill_actions(
    engine: Engine,
    source: Any,
    identities: dict[str, UUID],
    report: BackfillReport,
) -> None:
    """Splits and dividends for the same symbols, in the same run.

    Separate from the price pass rather than interleaved, because
    `backfill_daily_history` owns the checkpoint and the concurrency for
    prices and there is no hook to add two more requests inside it. The
    cost of the separation is that an interruption during this pass
    re-fetches actions on the next run — which is cheap and idempotent,
    since the writes are `ON CONFLICT DO NOTHING` over an append-only
    table.

    A symbol whose actions fail is recorded and the pass continues: its
    prices are already written and are still worth having, unadjusted, in
    a way the report makes visible.
    """
    for symbol, security_id in sorted(identities.items()):
        actions = []
        try:
            for fetch in (source.fetch_splits, source.fetch_dividends):
                result = await fetch(symbol)
                report.requests += 1
                actions.extend(result.records)
        except FmpError as error:
            report.failed[f"{symbol}:actions"] = f"{type(error).__name__}: {error}"
            continue

        if not actions:
            continue
        try:
            _persist(engine, security_id, [], actions, report)
        except Exception as error:  # noqa: BLE001 - one security must not stop the run
            report.failed[f"{symbol}:actions"] = f"{type(error).__name__}: {error}"


def _persist(
    engine: Engine,
    security_id: UUID,
    bars: list[Any],
    actions: list[Any],
    report: BackfillReport,
) -> None:
    """One security's records, in its own transaction.

    One transaction per security for the reason `prices.py` gives: a
    single transaction spanning ten thousand securities would hold locks
    for the length of the run and lose everything to one failure at the
    end.
    """
    with engine.begin() as connection:
        outcome = normalize_security(security_id=security_id, bars=bars, actions=actions)
        persist(outcome, CanonicalWriter(connection))

    written_bars = outcome.writes["bars"]
    written_actions = outcome.writes["corporate_actions"]
    report.bars_offered += written_bars.offered
    report.bars_inserted += written_bars.inserted
    report.actions_offered += written_actions.offered
    report.actions_inserted += written_actions.inserted
    if written_bars.inserted:
        report.securities_written += 1


def _iso_date(name: str, raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise BackfillNotConfigured(
            f"{name}={raw!r} is not an ISO date. Use YYYY-MM-DD."
        ) from error


async def _run_configured(
    engine: Engine,
    *,
    settings: BackfillSettings,
    scanner_settings: ScannerSettings | None = None,
    profile: DeploymentProfile | None = None,
) -> BackfillReport:
    """Resolve the symbol set, then run. The entrypoint's own body."""
    (profile or profile_for()).validate()

    if settings.symbols:
        # Named symbols still have to resolve to identities, and a symbol
        # ARGUS does not know is a typo rather than a security — reported
        # rather than silently skipped.
        identities = _identities_for(engine, settings.symbols)
    else:
        resolved = scanner_settings or ScannerSettings.from_environment()
        with engine.begin() as connection:
            universe_id = resolve_universe_version(connection, resolved.universe_version)
            members = universe_members(
                connection,
                universe_version_id=universe_id,
                as_of=datetime.now(UTC),
            )
        identities = {member.ticker: member.security_id for member in members.members}

    async with FmpClient() as client:
        return await run_backfill(
            engine, FmpFetcher(client), settings=settings, identities=identities
        )


def _identities_for(engine: Engine, symbols: tuple[str, ...]) -> dict[str, UUID]:
    """Current identities for explicitly named symbols.

    `try_resolve` rather than `resolve`: a symbol nobody has registered
    is an operator typo, and refusing the whole run for one is worse than
    naming it and continuing with the rest.
    """
    from data.normalization.identity import SecurityIdentityResolver

    found: dict[str, UUID] = {}
    unknown: list[str] = []
    with engine.begin() as connection:
        resolver = SecurityIdentityResolver(connection)
        for symbol in symbols:
            security_id = resolver.try_resolve(symbol, datetime.now(UTC))
            if security_id is None:
                unknown.append(symbol)
            else:
                found[symbol] = security_id

    if unknown:
        _log.warning(
            "some named symbols are not registered securities",
            extra={"event": "backfill_unknown_symbols", "symbols": sorted(unknown)},
        )
    return found


def main(argv: list[str] | None = None) -> int:
    """`python -m infra.deploy.backfill` — one historical load.

    Exit codes match the other entrypoints:

    - `0` history was written, or every symbol was already checkpointed.
    - `1` the job ran and wrote nothing — a range with no trading days, a
      universe with no members, or every symbol failing.
    - `2` it could not start: no range configured, or no universe.
    """
    refuse_arguments("infra.deploy.backfill", argv)
    configure_logging()
    profile = profile_for()

    try:
        settings = BackfillSettings.from_environment()
    except BackfillNotConfigured as not_configured:
        _log.error(
            "backfill is not configured",
            extra={"event": "backfill_not_configured", "detail": str(not_configured)},
        )
        return 2

    try:
        engine = create_db_engine(pool_pre_ping=True, pool_size=5, max_overflow=0)
        report = asyncio.run(_run_configured(engine, settings=settings, profile=profile))
    except ScannerNotReady as not_ready:
        _log.error(
            "backfill prerequisite missing",
            extra={"event": "backfill_not_ready", "detail": str(not_ready)},
        )
        return 2

    return 0 if report.healthy else 1


if __name__ == "__main__":  # pragma: no cover - the container's entrypoint
    raise SystemExit(main(sys.argv[1:]))
