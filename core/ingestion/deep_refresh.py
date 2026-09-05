"""The tiered per-security deep refresh: fundamentals, news, corporate actions.

Frequency is set by the security's *current* watchlist phase — 30 days in
DOWN_TREND, 10 in CONSOLIDATION, daily in BREAKOUT_READY and UPTREND. The
reasoning is in `config.py`'s rationales; the decision is `tiers.decide`;
this file does what the decision says.

## No deadline, and that is deliberate

`core/live_scanner/readiness.py` checks OHLCV coverage and nothing else,
so a lagging fundamentals or news refresh never blocks a scan. This half
of the run is therefore allowed to still be working when the scanner
fires. What it must *not* do is delay the half that does have a deadline,
which is why the orchestrator runs the two in sequence rather than
concurrently: both draw on the same FMP per-minute budget, so overlapping
them would spend the price pull's margin on news.

## Phase is read live, every run

`current_phases` queries the `market_state` projection at decision time.
The log table records which phase drove a completed refresh — a
historical fact — and nothing reads that as an answer to "what phase is
this security in now". `phases.py` says why at length.

## One transaction per security

Fetch, then write fundamentals, news and the log row together. Together
because the log row is what says "this security has been refreshed", and
a log row committed without its data would make the next run skip a
security that has none. If the transaction fails, nothing is logged and
the security is due again tomorrow, which is the right way round.

## Corporate actions ride this cadence, and that is a cost decision

Splits and dividends are not extra colour like news — they are what makes
the *price series itself* correct. Module 08's `load_panel` builds its
adjustment factors from `canonical_corporate_actions` at load time rather
than storing an adjusted series, so an empty table makes an unadjusted
2-for-1 split look like a −50% single-bar collapse, and Module 15 records
a successful setup as a catastrophic failure. That was issue G2.

They are here rather than in `prices.py` because FMP exposes splits and
dividends **per symbol only** — there is no calendar or bulk endpoint. A
daily full-universe sweep would be two extra requests per symbol per day
and would roughly triple the price path's volume. Riding this file's
existing 30/10/1/1 cadence instead spends the requests where they matter:
a name in BREAKOUT_READY is checked daily, a name in DOWN_TREND monthly.

That ordering is the right one for ARGUS specifically. A missed split on
a security nobody is trading distorts a chart; a missed split on a
security at the edge of a breakout distorts the outcome record that
Module 15 learns from.

**What it costs.** A security in DOWN_TREND can carry a stale adjustment
for up to 30 days. The raw series is never wrong — only the adjusted one
— and a stale adjustment on a name that far from a setup is a chart
artefact rather than a corrupted outcome. The alternative was paying
daily for every name in the universe to close a window that only matters
for a handful of them.

## Articles are filtered to the symbol they were requested for

FMP's news endpoint takes a symbol list and its rows carry their own
`symbol`. Anything that comes back under a different one is dropped
rather than attributed to the security that was asked about: a news row
on the wrong company is worse than a missing one, because Module 19
displays it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from sqlalchemy import Engine

from core.ingestion.config import IngestionConfig
from core.ingestion.members import MemberSet, UniverseMember
from core.ingestion.news_writer import write_news
from core.ingestion.phases import current_phases
from core.ingestion.refresh_log import CompletedRefresh, last_refreshes, record_refresh
from core.ingestion.tiers import DueDecision, decide
from data.normalization.persistence import CanonicalWriter
from data.normalization.pipeline import normalize_security, persist
from data.normalization.translate import TranslationError, translate_news
from data.provider_adapters.fmp.errors import FmpError
from data.provider_adapters.fmp.models import CorporateAction, FinancialStatement, NewsArticle
from infra.db.enums import MarketState
from infra.observability.logging import get_logger

__all__ = ["DeepRefreshReport", "DeepRefreshSource", "refresh_due_securities"]

_log = get_logger("argus.ingestion.deep_refresh")


class DeepRefreshSource(Protocol):
    """The slice of Module 04's `FmpFetcher` this path uses."""

    async def fetch_financial_statement(
        self,
        symbol: str,
        statement_type: str,
        *,
        period: str = ...,
        limit: int = ...,
    ) -> Any: ...

    async def fetch_news(self, symbols: Any, *, page: int = ..., limit: int = ...) -> Any: ...

    async def fetch_splits(self, symbol: str) -> Any: ...

    async def fetch_dividends(self, symbol: str) -> Any: ...


@dataclass(slots=True)
class DeepRefreshReport:
    """What one day's tiered refresh decided and wrote."""

    trading_date: date
    considered: int = 0
    due: int = 0
    refreshed: int = 0
    triggers: dict[str, int] = field(default_factory=dict)
    fundamentals_inserted: int = 0
    news_inserted: int = 0
    corporate_actions_inserted: int = 0
    requests: int = 0
    failed: dict[str, str] = field(default_factory=dict)

    def count(self, trigger: str) -> None:
        self.triggers[trigger] = self.triggers.get(trigger, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date.isoformat(),
            "considered": self.considered,
            "due": self.due,
            "refreshed": self.refreshed,
            "triggers": dict(self.triggers),
            "fundamentals_inserted": self.fundamentals_inserted,
            "news_inserted": self.news_inserted,
            "corporate_actions_inserted": self.corporate_actions_inserted,
            "requests": self.requests,
            "failed": len(self.failed),
        }


@dataclass(frozen=True, slots=True)
class _Fetched:
    """One security's payload for this refresh.

    A record rather than a growing tuple: three parallel lists unpacked
    positionally is where the wrong one gets passed to the wrong
    parameter, and `normalize_security` takes all three by keyword.
    """

    statements: list[FinancialStatement]
    articles: list[NewsArticle]
    actions: list[CorporateAction]


@dataclass(frozen=True, slots=True)
class _DueSecurity:
    member: UniverseMember
    state: MarketState
    watchlist: str
    decision: DueDecision


async def refresh_due_securities(
    engine: Engine,
    source: DeepRefreshSource,
    *,
    members: MemberSet,
    trading_date: date,
    config: IngestionConfig | None = None,
    max_concurrency: int = 1,
    now: datetime | None = None,
) -> DeepRefreshReport:
    """Refresh every universe member whose tier says it is due today."""
    resolved = config or IngestionConfig()
    report = DeepRefreshReport(trading_date=trading_date)
    report.considered = len(members.members)

    if not members.members:
        return report

    identities = [member.security_id for member in members.members]
    with engine.begin() as connection:
        phases = current_phases(connection, identities, resolved.settings)
        previous = last_refreshes(connection, identities)

    outstanding: list[_DueSecurity] = []
    for member in members.members:
        phase = phases.get(member.security_id)
        watchlist = phase[1] if phase is not None else None
        decision = decide(
            watchlist=watchlist,
            last=previous.get(member.security_id),
            target_date=trading_date,
            settings=resolved.settings,
        )
        report.count(decision.trigger)
        if decision.due and phase is not None:
            outstanding.append(
                _DueSecurity(
                    member=member,
                    state=phase[0],
                    watchlist=phase[1],
                    decision=decision,
                )
            )

    report.due = len(outstanding)
    if not outstanding:
        _log.info(
            "no security is due a deep refresh",
            extra={"event": "deep_refresh_none_due", **report.as_dict()},
        )
        return report

    moment = now or datetime.now(UTC)
    semaphore = asyncio.Semaphore(max(max_concurrency, 1))

    async def refresh_one(due: _DueSecurity) -> None:
        async with semaphore:
            try:
                fetched = await _fetch(source, due.member.ticker, resolved, report)
            except FmpError as error:
                report.failed[due.member.ticker] = f"{type(error).__name__}: {error}"
                return
        try:
            _write(engine, due, fetched, resolved, report, moment=moment)
        except Exception as error:  # noqa: BLE001 - one security must not stop the run
            report.failed[due.member.ticker] = f"{type(error).__name__}: {error}"
            _log.warning(
                "deep refresh persist failed",
                extra={
                    "event": "deep_refresh_persist_failed",
                    "ticker": due.member.ticker,
                    "error_type": type(error).__name__,
                },
            )

    await asyncio.gather(*(refresh_one(due) for due in outstanding))

    _log.info(
        "tiered deep refresh finished",
        extra={"event": "deep_refresh_finished", **report.as_dict()},
    )
    return report


async def _fetch(
    source: DeepRefreshSource,
    ticker: str,
    config: IngestionConfig,
    report: DeepRefreshReport,
) -> _Fetched:
    """Every configured statement type, then news, then splits and dividends.

    Sequential rather than gathered: the concurrency ceiling belongs to
    the run as a whole (`max_concurrency` securities at once), and
    fanning out inside one security as well would make the real ceiling
    the product of the two and quietly exceed the rate limiter's budget.

    An `FmpError` on any of these — corporate actions included —
    propagates and costs the security its whole refresh. That is
    deliberate rather than an oversight of the alternative: swallowing a
    splits failure would still write the log row, and the log row is what
    says "this security has been refreshed". A security in DOWN_TREND
    would then wait 30 days to retry a split it never fetched, which is
    exactly the silence G2 was about. Failing the whole refresh makes it
    due again tomorrow.
    """
    statements: list[FinancialStatement] = []
    for statement_type in config.statement_types:
        result = await source.fetch_financial_statement(
            ticker,
            statement_type,
            period=config.statement_period,
            limit=config.settings.statements,
        )
        report.requests += 1
        statements.extend(result.records)

    news = await source.fetch_news([ticker], limit=config.settings.articles)
    report.requests += 1

    # One symbol per request rather than a batch. FMP accepts a symbol
    # list, which would cut the request count sharply, but the article
    # limit is shared across the batch — so one heavily-covered name
    # would crowd out every quiet one it travelled with. The cost of
    # asking separately is in the volume formula in the README.
    articles = [article for article in news.records if _matches(article, ticker)]

    # Splits and dividends: two requests, both per-symbol because FMP
    # offers no calendar endpoint for either. See the module docstring on
    # why they ride this cadence rather than the daily price path.
    actions: list[CorporateAction] = []
    for fetch_actions in (source.fetch_splits, source.fetch_dividends):
        result = await fetch_actions(ticker)
        report.requests += 1
        actions.extend(result.records)

    return _Fetched(statements=statements, articles=articles, actions=actions)


def _matches(article: NewsArticle, ticker: str) -> bool:
    """Whether this article is about the symbol it was requested for."""
    if article.symbol is None:
        return False
    return article.symbol.strip().upper() == ticker.strip().upper()


def _write(
    engine: Engine,
    due: _DueSecurity,
    fetched: _Fetched,
    config: IngestionConfig,
    report: DeepRefreshReport,
    *,
    moment: datetime,
) -> None:
    """Fundamentals, corporate actions, news and the log row, in one transaction."""
    security_id = due.member.security_id

    with engine.begin() as connection:
        # `actions=` is what closes G2. No bars are passed, so nothing is
        # adjusted here — `persist` writes the actions to
        # `canonical_corporate_actions` and Module 08's `load_panel`
        # builds its factors from them at read time, which is where the
        # adjustment belongs.
        outcome = normalize_security(
            security_id=security_id,
            statements=fetched.statements,
            actions=fetched.actions,
        )
        persist(outcome, CanonicalWriter(connection))
        fundamentals = outcome.writes["fundamentals"]
        corporate_actions = outcome.writes["corporate_actions"]

        canonical_articles = []
        for article in fetched.articles:
            try:
                canonical_articles.append(translate_news(article, security_id))
            except TranslationError as error:
                # An article with no timestamp or no headline. Rejected
                # rather than written with a fabricated one, the same
                # rule Module 05 applies to every other record type.
                report.failed[f"{due.member.ticker}:news"] = str(error)
        written_news = write_news(connection, canonical_articles)

        logged = record_refresh(
            connection,
            CompletedRefresh(
                security_id=security_id,
                refreshed_on=report.trading_date,
                triggering_watchlist=due.watchlist,
                market_state=due.state,
                trigger=due.decision.trigger,
                statements_written=fundamentals.inserted,
                news_written=written_news.inserted,
                config_version_label=config.version_label(),
                detail={
                    "reason": due.decision.reason,
                    "ticker": due.member.ticker,
                    "statement_types": list(config.statement_types),
                    "statements_offered": fundamentals.offered,
                    "news_offered": written_news.offered,
                    # Not a column on `deep_refresh_log`: the table's
                    # written-counts are fundamentals and news, and
                    # adding a third would be a migration for a number
                    # the run report already carries. Recorded in the
                    # detail payload so a past refresh can still be
                    # asked what it saw.
                    "corporate_actions_offered": corporate_actions.offered,
                    "corporate_actions_written": corporate_actions.inserted,
                    "completed_at": moment.isoformat(),
                    "translation_rejected": len(outcome.translation.rejected),
                },
            ),
        )

    report.fundamentals_inserted += fundamentals.inserted
    report.news_inserted += written_news.inserted
    report.corporate_actions_inserted += corporate_actions.inserted
    if logged:
        report.refreshed += 1
