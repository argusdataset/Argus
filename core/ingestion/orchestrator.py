"""One daily ingestion run: prices first, then the tiered deep refresh.

## The order is a decision, not an implementation detail

Both halves draw on the same FMP per-minute budget, and only one of them
has a deadline. `core/live_scanner/readiness.py` refuses to scan a day
whose OHLCV coverage is short, so the price pull must finish before the
scanner fires; nothing anywhere checks how fresh a fundamental is, so the
deep refresh may still be working afterwards.

Running them concurrently would spend the price pull's margin on news.
So: sequential, prices first, and the deep refresh gets whatever time is
left. `infra/deploy/ingestion.py` schedules the process ninety minutes
before the scanner for the same reason.

## The target session is the one the scanner will scan

Read from Module 18's own `scan_date_for(now)` rather than computed here.
That is deliberate: if this module decided independently which session to
fetch, the two could disagree, and a disagreement would look exactly like
a provider delay — the scanner asking about a date this job never
fetched, recording `DATA_NOT_READY`, and nothing anywhere naming the
cause. Reusing the function makes the two agree by construction.

Note the consequence, which surprises on first reading: at 21:00 UTC on a
Tuesday, the session the scanner is due to scan is **Monday's**, not
Tuesday's. Module 18's cutoff for a session is its close plus
`scan_offset_hours`, which for Tuesday has not arrived yet. That is the
conservative direction and it works in ARGUS's favour here — the data
being fetched is a day old and certainly published.

## What "healthy" means

The run ends by asking Module 18's own `check_readiness` whether the
session it just ingested is now scannable. Not a threshold of this
module's own invention: the only opinion that matters is the one the
scanner will act on, and asking the scanner's own question is the only
way to be sure the answer is yes.

**For a while the answer was no, for a reason outside this module.**
Module 05 derives a daily bar's `availability_time` as the session close
plus sixteen hours; Module 18's PIT cutoff for that same session was the
close plus five (`scan_offset_hours`). Sixteen is greater than five, so a
bar this module wrote was not yet *knowable* at the cutoff the readiness
check applied, and coverage read as zero however complete the ingestion
was. The fix was one number in Module 18's config — `scan_offset_hours`
raised to seventeen, one past the bar lag, with `readiness_window_hours`
raised alongside it so the retry margin the offset spends did not go
with it (see `core/live_scanner/config.py`'s own rationale text for both).
This module was told not to touch that file, so the finding is recorded
in `docs/architecture/KNOWN_ISSUES.md` G1 (RESOLVED) and in
`core/ingestion/README.md`, which sets out the arithmetic, and
`tests/integration/ingestion/test_readiness_handoff.py`, which pins the
fix against a real ingested day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine

from core.ingestion.config import IngestionConfig
from core.ingestion.deep_refresh import (
    DeepRefreshReport,
    DeepRefreshSource,
    refresh_due_securities,
)
from core.ingestion.members import universe_members
from core.ingestion.ownership import (
    OwnershipIngestReport,
    due_members,
    ingest_filings,
    ingest_ownership,
)
from core.ingestion.phases import current_phases
from core.ingestion.prices import OhlcvReport, PriceSource, ingest_daily_prices
from core.ingestion.refresh_log import last_refreshes
from core.ingestion.strategy import StrategyDecision, select_strategy
from core.live_scanner.readiness import ReadinessReport, check_readiness
from core.live_scanner.schedule import as_of_for, scan_date_for
from infra.observability.logging import get_logger
from packages.config import AppConfig, get_config

__all__ = ["IngestionReport", "IngestionSource", "run_daily_ingestion"]

_log = get_logger("argus.ingestion.run")


class IngestionSource(PriceSource, DeepRefreshSource, Protocol):
    """Both halves of the provider surface, from one client.

    One object, not two, and that is the requirement rather than a
    convenience: `FmpClient` owns the token buckets, so two clients would
    be two independent budgets against a per-minute ceiling FMP enforces
    once. Passing a single source through both phases is what makes them
    share it.
    """


@dataclass(slots=True)
class IngestionReport:
    """Everything one run did, and whether the scanner can act on it."""

    trading_date: date | None
    strategy: StrategyDecision | None = None
    prices: OhlcvReport | None = None
    deep_refresh: DeepRefreshReport | None = None
    #: 8-K filings, Form 4 transactions and 13F summaries — the raw data
    #: Modules 28 and 29 read. `None` when the provider cannot serve those
    #: endpoints; see `core/ingestion/ownership.py`.
    ownership: OwnershipIngestReport | None = None
    readiness: ReadinessReport | None = None
    skipped_reason: str | None = None

    @property
    def healthy(self) -> bool:
        """True when the session this run fetched is now scannable.

        A run that fetched nothing because no session was due is healthy:
        there was nothing to do and it correctly did nothing.
        """
        if self.trading_date is None:
            return True
        return self.readiness is not None and self.readiness.ready

    def as_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date.isoformat() if self.trading_date else None,
            "strategy": self.strategy.as_dict() if self.strategy else None,
            "prices": self.prices.as_dict() if self.prices else None,
            "deep_refresh": self.deep_refresh.as_dict() if self.deep_refresh else None,
            "ownership": self.ownership.as_dict() if self.ownership else None,
            "readiness": self.readiness.as_dict() if self.readiness else None,
            "skipped_reason": self.skipped_reason,
            "healthy": self.healthy,
        }


async def run_daily_ingestion(
    engine: Engine,
    source: IngestionSource,
    *,
    universe_version_id: UUID,
    config: IngestionConfig | None = None,
    app_config: AppConfig | None = None,
    now: datetime | None = None,
) -> IngestionReport:
    """One scheduled wake-up: fetch the due session, then refresh what is due.

    `source` is a single object satisfying both provider protocols —
    Module 04's `FmpFetcher` does — so that both phases draw on one
    client and therefore one set of token buckets.
    """
    resolved = config or IngestionConfig()
    settings = app_config or get_config()
    moment = now or datetime.now(UTC)

    trading_date = scan_date_for(moment)
    if trading_date is None:
        report = IngestionReport(
            trading_date=None,
            skipped_reason=(
                "No trading session is due yet. Module 18's schedule answers 'which "
                "session should have been handled by now', and on a weekend or "
                "before the first cutoff of the week the answer is none."
            ),
        )
        _log.info(
            "no session due, nothing to ingest",
            extra={"event": "ingestion_skipped", **report.as_dict()},
        )
        return report

    as_of = as_of_for(trading_date)
    decision = select_strategy(settings.providers, resolved)

    _log.info(
        "daily ingestion starting",
        extra={
            "event": "ingestion_starting",
            "trading_date": trading_date.isoformat(),
            "as_of": as_of.isoformat(),
            "universe_version_id": str(universe_version_id),
            "config_version_label": resolved.version_label(),
            **decision.as_dict(),
        },
    )

    with engine.begin() as connection:
        members = universe_members(
            connection,
            universe_version_id=universe_version_id,
            as_of=as_of,
        )

    prices = await ingest_daily_prices(
        engine,
        source,
        members=members,
        trading_date=trading_date,
        decision=decision,
        config=resolved,
    )

    readiness = _readiness(engine, trading_date, as_of, universe_version_id)

    deep = await refresh_due_securities(
        engine,
        source,
        members=members,
        trading_date=trading_date,
        config=resolved,
        max_concurrency=settings.providers.fmp_max_concurrency,
        now=moment,
    )

    ownership = await _ingest_ownership_data(
        engine,
        source,
        members=members,
        trading_date=trading_date,
        config=resolved,
        max_concurrency=settings.providers.fmp_max_concurrency,
    )

    report = IngestionReport(
        trading_date=trading_date,
        strategy=decision,
        prices=prices,
        deep_refresh=deep,
        ownership=ownership,
        readiness=readiness,
    )
    _log.info(
        "daily ingestion finished",
        extra={"event": "ingestion_finished", **report.as_dict()},
    )
    return report


async def _ingest_ownership_data(
    engine: Engine,
    source: Any,
    *,
    members: Any,
    trading_date: date,
    config: IngestionConfig,
    max_concurrency: int,
) -> OwnershipIngestReport:
    """The Ultimate-plan half: 8-K in bulk, then Form 4 and 13F per due name.

    Last in the run, and deliberately so. Like the deep refresh it has no
    deadline — `core/live_scanner/readiness.py` checks OHLCV coverage and
    nothing else, so nothing downstream waits on these — and unlike the
    price pull it must not be allowed to spend the margin the scanner
    depends on.

    Which securities are "due" is Module 26's own tier decision, read from
    the same `market_state` projection and the same refresh log the
    fundamentals refresh reads, so the two cadences cannot drift apart.
    """
    outcome = OwnershipIngestReport(trading_date=trading_date)

    await ingest_filings(engine, source, members=members, trading_date=trading_date, report=outcome)

    identities = [member.security_id for member in members.members]
    with engine.begin() as connection:
        phases = current_phases(connection, identities, config.settings)
        previous = last_refreshes(connection, identities)

    due = due_members(
        members,
        phases=phases,
        previous=previous,
        trading_date=trading_date,
        config=config,
    )
    return await ingest_ownership(
        engine,
        source,
        due=due,
        trading_date=trading_date,
        config=config,
        report=outcome,
        max_concurrency=max_concurrency,
    )


def _readiness(
    engine: Engine,
    trading_date: date,
    as_of: datetime,
    universe_version_id: UUID,
) -> ReadinessReport:
    """Module 18's own question, asked with Module 18's own cutoff.

    Read-only, and the dependency runs the right way: this module asks
    the scanner whether its work was sufficient. Nothing is added to the
    scanner, and the scanner learns nothing about this module.
    """
    with engine.connect() as connection:
        return check_readiness(
            connection,
            scan_date=trading_date,
            as_of=as_of,
            universe_version_id=universe_version_id,
        )
