"""Fixtures for Module 26: a real database and a fake provider.

## Why the database is real

Everything this module decides turns on stored state — whether a bar is
already held, when a security was last refreshed, what phase it is in
now. A stubbed connection would prove none of it, and two of the
guarantees under test are database behaviour: the log table's unique
constraint, and `ON CONFLICT DO NOTHING` against two *partial* indexes on
`canonical_news`.

## Why the provider is fake

The opposite reason. Module 04 is already tested against its own
recorded fixtures; what is under test here is what this module does with
what comes back. A recording provider also makes the interesting
assertion possible: *how many requests were made*, which is the whole
substance of "a re-run is a no-op".
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.market_state.thresholds import publish_target_model_version
from core.model_validation_evaluation.validation.replay import ModuleConfigs
from data.canonical_model.exchanges import CanonicalExchange
from data.canonical_model.pit import session_close
from data.normalization.identity import SecurityIdentityResolver
from data.provider_adapters.fmp.checkpoint import JobCheckpoint
from data.provider_adapters.fmp.fetchers import BackfillReport, FetchResult
from data.provider_adapters.fmp.models import (
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    FetchProvenance,
    FinancialStatement,
    NewsArticle,
)
from infra.db.enums import MarketState
from infra.db.schema.identity import universe_membership, universe_version
from infra.db.schema.intelligence import market_state
from packages.config.settings import AppConfig, DatabaseSettings, ProvidersSettings
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)

#: A plain Tuesday, and the Monday before it. The session Module 18 is
#: due to scan at `NOW` is the Monday — see the orchestrator's docstring
#: on why the answer is a day behind the clock.
TARGET_DATE = date(2026, 3, 9)
NEXT_DATE = date(2026, 3, 10)
#: Late on the Tuesday, after Monday's cutoff and before Tuesday's.
NOW = datetime(2026, 3, 10, 21, 0, tzinfo=UTC)

LISTED_FROM = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than DELETE: the canonical tables are append-only, so
    a delete-based teardown would be refused by Module 03's triggers.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def committing_engine(engine: Engine) -> Iterator[Engine]:  # noqa: F811
    """The engine itself, for tests that run the real ingestion path.

    The ingestion code opens its own transactions per security, on
    purpose, so it cannot be driven inside one rolled-back outer
    transaction. These tests therefore commit, and clean up by working
    only on securities they created in this test's own universe version.
    """
    yield engine


@pytest.fixture
def universe(engine: Engine) -> Callable[..., Any]:  # noqa: F811
    """Register securities, put them in a universe, and give them a state."""

    def _build(
        tickers: tuple[str, ...],
        *,
        states: dict[str, MarketState] | None = None,
        label: str | None = None,
    ) -> tuple[UUID, dict[str, UUID]]:
        stamp = datetime.now(UTC).timestamp()
        with engine.begin() as conn:
            version_id = conn.execute(
                universe_version.insert()
                .values(
                    version_label=label or f"module26-{stamp}",
                    as_of_date=datetime.combine(TARGET_DATE, datetime.min.time(), tzinfo=UTC),
                    definition={"source": "module26 integration test"},
                )
                .returning(universe_version.c.id)
            ).scalar_one()

            model_version = publish_target_model_version(
                conn, ModuleConfigs().market_state, description="module26 test"
            )

            resolver = SecurityIdentityResolver(conn)
            identities: dict[str, UUID] = {}
            for ticker in tickers:
                security_id = resolver.register(
                    f"{ticker}{int(stamp * 1000) % 100000}",
                    exchange=CanonicalExchange.NASDAQ,
                    valid_from=LISTED_FROM,
                    name=f"{ticker} Test Corp.",
                )
                identities[ticker] = security_id
                conn.execute(
                    universe_membership.insert().values(
                        universe_version_id=version_id,
                        security_id=security_id,
                        listing_status="LISTED",
                        listed_from=LISTED_FROM,
                        listed_to=None,
                        exchange="NASDAQ",
                        interval_evidence="reported",
                    )
                )
                state = (states or {}).get(ticker)
                if state is not None:
                    conn.execute(
                        market_state.insert().values(
                            security_id=security_id,
                            state=state.value,
                            entered_at=LISTED_FROM,
                            target_model_version_id=model_version,
                        )
                    )
        return version_id, identities

    return _build


@pytest.fixture
def tickers_for(engine: Engine) -> Callable[[dict[str, UUID]], dict[UUID, str]]:  # noqa: F811
    """The registered ticker for each identity, since the fixture suffixes them."""
    from infra.db.schema.identity import security_ticker_history

    def _lookup(identities: dict[str, UUID]) -> dict[UUID, str]:
        from sqlalchemy import select

        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    security_ticker_history.c.security_id, security_ticker_history.c.ticker
                ).where(security_ticker_history.c.security_id.in_(list(identities.values())))
            ).all()
        return {row.security_id: row.ticker for row in rows}

    return _lookup


@pytest.fixture
def checkpoint_dir(tmp_path) -> Any:
    """A per-test checkpoint directory.

    Per test rather than the real `.cache/` location, because otherwise
    the "resumes from checkpoint" assertion would depend on what the
    previous test left behind.
    """
    return tmp_path / "checkpoints"


@pytest.fixture
def app_config(tmp_path) -> AppConfig:
    """Config built directly, never loaded from the environment.

    `get_config()` reads `.env` and requires a database block; a test
    that let it do that would be asserting against whatever machine it
    ran on. Passed explicitly into `run_daily_ingestion` instead, which
    takes it as a parameter for exactly this reason.
    """
    return AppConfig(
        database=DatabaseSettings(host="127.0.0.1", port=5432, name="argus_test", user="argus"),
        providers=ProvidersSettings(fmp_checkpoint_dir=str(tmp_path / "checkpoints")),
    )


class FakeFetcher:
    """A recording stand-in for Module 04's `FmpFetcher`.

    Satisfies both provider protocols. Counts every request, which is
    what makes "no doubled FMP requests" an assertion rather than a hope,
    and reproduces `backfill_daily_history`'s contract closely enough for
    the checkpoint behaviour under test: skip completed units, record
    each as it finishes, call `on_records` per symbol.
    """

    def __init__(
        self,
        *,
        bars_for: Callable[[str], list[DailyBar]] | None = None,
        statements_for: Callable[[str, str], list[FinancialStatement]] | None = None,
        news_for: Callable[[str], list[NewsArticle]] | None = None,
        actions_for: Callable[[str, CorporateActionKind], list[CorporateAction]] | None = None,
        checkpoint_dir: Any = None,
        fail_after: int | None = None,
        bulk_symbols: tuple[str, ...] = (),
    ) -> None:
        self._bars_for = bars_for or (lambda symbol: [])
        self._statements_for = statements_for or (lambda symbol, kind: [])
        self._news_for = news_for or (lambda symbol: [])
        self._actions_for = actions_for or (lambda symbol, kind: [])
        self._checkpoint_dir = checkpoint_dir
        self._fail_after = fail_after
        self._bulk_symbols = bulk_symbols
        self.price_requests: list[str] = []
        self.statement_requests: list[tuple[str, str]] = []
        self.news_requests: list[str] = []
        #: (symbol, kind) per split/dividend request. Separate from the
        #: statement list because the volume question G2 turned on is
        #: "how many extra requests do corporate actions cost", and a
        #: shared counter could not answer it.
        self.action_requests: list[tuple[str, CorporateActionKind]] = []
        self.bulk_requests: list[date] = []

    # -- prices -------------------------------------------------------------

    async def fetch_daily_history(self, symbol: str, *, start=None, end=None):
        self.price_requests.append(symbol)
        return _result(self._bars_for(symbol))

    async def fetch_eod_for_date(self, as_of: date):
        self.bulk_requests.append(as_of)
        bars: list[DailyBar] = []
        for symbol in self._bulk_symbols:
            bars.extend(self._bars_for(symbol))
        return _result(bars)

    async def backfill_daily_history(
        self,
        symbols,
        *,
        job_name: str = "daily_history_backfill",
        start=None,
        end=None,
        on_records=None,
    ) -> BackfillReport:
        checkpoint = JobCheckpoint(self._checkpoint_dir, job_name)
        checkpoint.load()

        report = BackfillReport(requested=len(symbols))
        pending = checkpoint.pending(list(symbols))
        report.already_complete = report.requested - len(pending)

        for index, symbol in enumerate(pending):
            if self._fail_after is not None and index >= self._fail_after:
                raise InterruptedIngestion(symbol)
            result = await self.fetch_daily_history(symbol, start=start, end=end)
            if on_records is not None:
                on_records(result)
            report.succeeded += 1
            if not result.records:
                report.empty.append(symbol)
            checkpoint.record(symbol, succeeded=True, bars=len(result.records))

        return report

    # -- deep refresh -------------------------------------------------------

    async def fetch_financial_statement(
        self, symbol: str, statement_type: str, *, period: str = "annual", limit: int = 200
    ):
        self.statement_requests.append((symbol, statement_type))
        return _result(self._statements_for(symbol, statement_type))

    async def fetch_news(self, symbols, *, page: int = 0, limit: int = 50):
        self.news_requests.extend(symbols)
        articles: list[NewsArticle] = []
        for symbol in symbols:
            articles.extend(self._news_for(symbol))
        return _result(articles)

    async def fetch_splits(self, symbol: str):
        self.action_requests.append((symbol, CorporateActionKind.SPLIT))
        return _result(self._actions_for(symbol, CorporateActionKind.SPLIT))

    async def fetch_dividends(self, symbol: str):
        self.action_requests.append((symbol, CorporateActionKind.DIVIDEND))
        return _result(self._actions_for(symbol, CorporateActionKind.DIVIDEND))

    @property
    def total_requests(self) -> int:
        return (
            len(self.price_requests)
            + len(self.statement_requests)
            + len(self.news_requests)
            + len(self.action_requests)
            + len(self.bulk_requests)
        )


class InterruptedIngestion(RuntimeError):
    """Stands in for the process being killed mid-universe."""


def _result(records: list[Any]) -> FetchResult[Any]:
    return FetchResult(records=records, provenance=provenance(), empty_reason=None)


def provenance(endpoint: str = "test") -> FetchProvenance:
    return FetchProvenance(
        provider="fmp",
        endpoint=endpoint,
        url_path=f"/stable/{endpoint}",
        fetched_at=NOW,
        request_params={},
    )


def daily_bar(symbol: str, bar_date: date, *, close: float = 100.0) -> DailyBar:
    price = Decimal(str(close))
    return DailyBar(
        provenance=provenance("historical_price_eod_full"),
        symbol=symbol,
        bar_date=bar_date,
        open=price,
        high=price * Decimal("1.01"),
        low=price * Decimal("0.99"),
        close=price,
        volume=1_000_000,
    )


def session_bars(symbol: str, dates: tuple[date, ...]) -> list[DailyBar]:
    return [daily_bar(symbol, day) for day in dates]


def statement(
    symbol: str,
    statement_type: str,
    *,
    fiscal_date: date = date(2025, 12, 31),
    accepted: datetime | None = None,
) -> FinancialStatement:
    return FinancialStatement(
        provenance=provenance("income_statement"),
        symbol=symbol,
        statement_type=statement_type,
        fiscal_date=fiscal_date,
        period="FY",
        reported_currency="USD",
        accepted_date=accepted
        if accepted is not None
        else datetime(2026, 2, 20, 21, 5, tzinfo=UTC),
        data={"revenue": 1_000_000},
    )


#: Distinguishes "no url argument given" from "url is deliberately None",
#: which is the whole difference between the two partial unique indexes
#: on `canonical_news`.
UNSET = object()


def article(symbol: str, *, published: datetime | None = None, url: Any = UNSET) -> NewsArticle:
    moment = published or datetime(2026, 3, 9, 14, 30, tzinfo=UTC)
    return NewsArticle(
        provenance=provenance("stock_news"),
        symbol=symbol,
        published_at=moment,
        title=f"{symbol} reports something",
        site="example.test",
        url=f"https://example.test/{symbol}" if url is UNSET else url,
        text="A summary.",
    )


def close_of(day: date) -> datetime:
    return session_close(day)


def days_before(day: date, count: int) -> date:
    return day - timedelta(days=count)


def split(
    symbol: str,
    *,
    event_date: date,
    numerator: int = 2,
    denominator: int = 1,
) -> CorporateAction:
    """One split, as FMP reports them.

    FMP supplies no announcement date for a split, so none is set here
    either — `translate_corporate_action` then treats it as knowable only
    on the effective date, and a fixture that invented one would be
    testing a payload the provider does not send.
    """
    return CorporateAction(
        provenance=provenance("splits"),
        symbol=symbol,
        kind=CorporateActionKind.SPLIT,
        event_date=event_date,
        details={"numerator": numerator, "denominator": denominator},
    )


def dividend(
    symbol: str,
    *,
    event_date: date,
    amount: str = "0.25",
    declared_on: date | None = None,
) -> CorporateAction:
    """One cash dividend, with the declaration date FMP does supply."""
    details: dict[str, Any] = {"dividend": amount, "adjDividend": amount}
    if declared_on is not None:
        details["declarationDate"] = declared_on.isoformat()
    return CorporateAction(
        provenance=provenance("dividends"),
        symbol=symbol,
        kind=CorporateActionKind.DIVIDEND,
        event_date=event_date,
        details=details,
    )
