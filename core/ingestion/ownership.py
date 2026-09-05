"""Fetching and storing the Ultimate-plan ownership data: 8-K, Form 4, 13F.

The ingestion half of Modules 28 and 29. Those modules compute signals
from stored rows and never touch a provider — that separation is what
lets `core/news_signals/orchestrator.py` state honestly that it needs no
FMP client and no secret. This file is where the provider *is* touched,
and it lives in Module 26 because Module 26 is where ingestion lives.

## Three data types, two fetch shapes, and why they differ

**8-K filings — one bulk request for the whole market.** The signal is
reactive and daily: a company that files today should be flagged today,
whatever phase it is in. Fetching per-symbol would have tied that to the
tiered refresh cadence, so a security sitting in DOWN_TREND would have
had its 8-K noticed up to thirty days late — which is most of a month
after the event stopped being news. `/stable/8k-latest` returns every
filer's recent filings in one paged request, so the whole universe is
covered for a handful of requests. Rows for symbols outside the universe
are dropped rather than stored: an unknown ticker has no `security_id` to
attach to, and inventing one would be worse than skipping it.

**Form 4 and 13F — per-symbol, tier-paced.** Neither endpoint has a bulk
form, so these cost one request per security. They are therefore paced by
the same tier decision the fundamentals refresh uses (`tiers.decide`):
daily for BREAKOUT_READY and UPTREND, ten days in CONSOLIDATION, thirty
in DOWN_TREND. That is a spending decision rather than a correctness one
and it is the same one Module 26 already made for news and fundamentals —
which is exactly why the cadence is read from that config rather than
invented here.

The cost of the tiering, stated plainly: an insider cluster forming in a
name still in DOWN_TREND is seen up to thirty days late. That is
acceptable in a way the 8-K delay was not, because a cluster is measured
over a thirty-day window in the first place — it is a slow signal, and
observing it slowly does not change what it says.

## Nothing here decides anything

Every function in this file fetches, translates and writes. The reading
of those rows — whether a cluster formed, whether ownership grew — stays
in `core/news_signals/` and `core/ownership_signals/`, which is why a
failure here degrades the signals to "undetermined" rather than to a
confident wrong answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine

from core.ingestion.config import IngestionConfig
from core.ingestion.members import MemberSet, UniverseMember
from core.ingestion.tiers import decide
from core.news_signals.filings import translate_filing, write_filings
from core.ownership_signals.config import OwnershipSignalConfig
from core.ownership_signals.insider import translate_insider_transaction, write_insider_trades
from core.ownership_signals.institutional import (
    translate_institutional_ownership,
    write_institutional_ownership,
)
from data.provider_adapters.fmp.errors import FmpError
from infra.observability.logging import get_logger

__all__ = [
    "FilingIngestSource",
    "OwnershipIngestReport",
    "OwnershipIngestSource",
    "due_members",
    "ingest_filings",
    "ingest_ownership",
]

_log = get_logger("argus.ingestion.ownership")


class FilingIngestSource(Protocol):
    """The slice of Module 04's fetcher the 8-K bulk path uses."""

    async def fetch_latest_8k_filings(self, *, page: int = ..., limit: int = ...) -> Any: ...


class OwnershipIngestSource(Protocol):
    """The slice of Module 04's fetcher the per-symbol paths use."""

    async def fetch_insider_trades(
        self, symbol: str, *, page: int = ..., limit: int = ...
    ) -> Any: ...

    async def fetch_institutional_ownership(
        self, symbol: str, *, year: int | None = ..., quarter: int | None = ...
    ) -> Any: ...


@dataclass(slots=True)
class OwnershipIngestReport:
    """What one ingestion pass fetched and wrote."""

    trading_date: date
    #: Securities the pass was asked to cover.
    considered: int = 0
    filings_offered: int = 0
    filings_written: int = 0
    filings_unmatched: int = 0
    insider_offered: int = 0
    insider_written: int = 0
    institutional_offered: int = 0
    institutional_written: int = 0
    requests: int = 0
    rejected: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    #: Set when the provider given to this run cannot serve these
    #: endpoints at all — see `ingest_filings`.
    skipped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date.isoformat(),
            "considered": self.considered,
            "filings_offered": self.filings_offered,
            "filings_written": self.filings_written,
            "filings_unmatched": self.filings_unmatched,
            "insider_offered": self.insider_offered,
            "insider_written": self.insider_written,
            "institutional_offered": self.institutional_offered,
            "institutional_written": self.institutional_written,
            "requests": self.requests,
            "rejected": len(self.rejected),
            "failed": len(self.failed),
            "skipped_reason": self.skipped_reason,
        }


async def ingest_filings(
    engine: Engine,
    source: FilingIngestSource,
    *,
    members: MemberSet,
    trading_date: date,
    report: OwnershipIngestReport | None = None,
    max_pages: int = 5,
    page_size: int = 100,
) -> OwnershipIngestReport:
    """Pull recent 8-K filings in bulk and store the ones ARGUS follows.

    A source that does not implement `fetch_latest_8k_filings` — the
    `FmpFetcher` predating the Ultimate plan, or a test double built
    before this existed — is *reported as skipped* rather than crashing
    the run. Reported rather than silently ignored, because a run whose
    filings never arrive should be visibly a run whose filings never
    arrived; the counters would otherwise read the same as a genuinely
    quiet day.
    """
    outcome = report or OwnershipIngestReport(trading_date=trading_date)
    if not hasattr(source, "fetch_latest_8k_filings"):
        outcome.skipped_reason = (
            "This provider does not implement fetch_latest_8k_filings, so no SEC "
            "filings were fetched. The 8-K signal will read False for every "
            "security until it does — see core/ingestion/ownership.py."
        )
        return outcome

    by_ticker = members.by_ticker()
    collected: list[Any] = []
    for page in range(max_pages):
        try:
            result = await source.fetch_latest_8k_filings(page=page, limit=page_size)
        except FmpError as error:
            outcome.failed["8k-latest"] = f"{type(error).__name__}: {error}"
            break
        outcome.requests += 1
        records = list(result.records)
        collected.extend(records)
        if len(records) < page_size:
            break

    translated = []
    for record in collected:
        security_id = by_ticker.get((record.symbol or "").strip().upper()) or by_ticker.get(
            record.symbol or ""
        )
        if security_id is None:
            # A filer outside this universe. Dropped, not stored: there is
            # no identity to attach it to.
            outcome.filings_unmatched += 1
            continue
        try:
            translated.append(translate_filing(record, security_id))
        except ValueError as error:
            outcome.rejected[f"8k:{record.symbol}"] = str(error)

    outcome.filings_offered += len(translated)
    if translated:
        with engine.begin() as connection:
            outcome.filings_written += write_filings(connection, translated).inserted

    _log.info(
        "sec filing ingestion finished",
        extra={"event": "filing_ingestion_finished", **outcome.as_dict()},
    )
    return outcome


async def ingest_ownership(
    engine: Engine,
    source: OwnershipIngestSource,
    *,
    due: list[UniverseMember],
    trading_date: date,
    config: IngestionConfig | None = None,
    ownership_config: OwnershipSignalConfig | None = None,
    report: OwnershipIngestReport | None = None,
    max_concurrency: int = 1,
) -> OwnershipIngestReport:
    """Fetch Form 4 and 13F for each due security, and store both.

    `due` is decided by the caller using Module 26's own tier logic — see
    the module docstring on why this is paced rather than run for the
    whole universe daily.

    One transaction per security, matching `deep_refresh.py`: a partial
    write for one name must not roll back another's, and a security whose
    fetch fails is simply due again next run.
    """
    resolved = config or IngestionConfig()
    thresholds = (ownership_config or OwnershipSignalConfig()).thresholds
    outcome = report or OwnershipIngestReport(trading_date=trading_date)
    outcome.considered = len(due)

    missing = [
        name
        for name in ("fetch_insider_trades", "fetch_institutional_ownership")
        if not hasattr(source, name)
    ]
    if missing:
        outcome.skipped_reason = (
            f"This provider does not implement {', '.join(missing)}, so no ownership "
            "data was fetched. The insider and 13F signals stay undetermined until "
            "it does — see core/ingestion/ownership.py."
        )
        return outcome

    if not due:
        return outcome

    semaphore = asyncio.Semaphore(max(max_concurrency, 1))

    async def one(member: UniverseMember) -> None:
        async with semaphore:
            try:
                trades = await source.fetch_insider_trades(
                    member.ticker, limit=int(resolved.settings.articles)
                )
                outcome.requests += 1
                holdings = await source.fetch_institutional_ownership(member.ticker)
                outcome.requests += 1
            except FmpError as error:
                outcome.failed[member.ticker] = f"{type(error).__name__}: {error}"
                return

        stored_trades = []
        for record in trades.records:
            try:
                stored_trades.append(
                    translate_insider_transaction(record, member.security_id, thresholds=thresholds)
                )
            except ValueError as error:
                outcome.rejected[f"insider:{member.ticker}"] = str(error)

        stored_holdings = []
        for record in holdings.records:
            try:
                stored_holdings.append(
                    translate_institutional_ownership(
                        record, member.security_id, thresholds=thresholds
                    )
                )
            except ValueError as error:
                outcome.rejected[f"13f:{member.ticker}"] = str(error)

        try:
            with engine.begin() as connection:
                insider_result = write_insider_trades(connection, stored_trades)
                institutional_result = write_institutional_ownership(connection, stored_holdings)
        except Exception as error:  # noqa: BLE001 - one security must not stop the run
            outcome.failed[member.ticker] = f"{type(error).__name__}: {error}"
            return

        outcome.insider_offered += insider_result.offered
        outcome.insider_written += insider_result.inserted
        outcome.institutional_offered += institutional_result.offered
        outcome.institutional_written += institutional_result.inserted

    await asyncio.gather(*(one(member) for member in due))

    _log.info(
        "ownership ingestion finished",
        extra={"event": "ownership_ingestion_finished", **outcome.as_dict()},
    )
    return outcome


def due_members(
    members: MemberSet,
    *,
    phases: dict[UUID, tuple[Any, str]],
    previous: dict[UUID, Any],
    trading_date: date,
    config: IngestionConfig,
) -> list[UniverseMember]:
    """Which securities are due an ownership refresh today.

    Reuses `core/ingestion/tiers.py`'s decision rather than restating the
    cadence, so a tier interval changed in `core/ingestion/config.py`
    moves this too. A security with no known phase is skipped: with no
    watchlist there is no tier, and inventing one here would put the
    cheapest possible cadence on a name nobody classified.
    """
    due: list[UniverseMember] = []
    for member in members.members:
        phase = phases.get(member.security_id)
        if phase is None:
            continue
        decision = decide(
            watchlist=phase[1],
            last=previous.get(member.security_id),
            target_date=trading_date,
            settings=config.settings,
        )
        if decision.due:
            due.append(member)
    return due
