"""The one-off historical load, and the corporate actions that ride with it.

Without this job, history accumulates one day per day forwards from
whenever ingestion first ran — and ARGUS needs 252 bars before a setup
can open, because `atr_percentile` is NaN below that window and both
detection states require it. So the first year produces watchlists and
Telegram alerts and no outcome record at all.

Three properties are under test, and the second is the one most easily
lost:

1. The range is honoured and the bars land.
2. **Corporate actions are fetched too.** Fifteen years of unadjusted
   prices is G2 with a longer reach: every split in that history is an
   uncorrected discontinuity that Module 15 reads as a catastrophic
   single-bar failure. Two extra requests per symbol remove the whole
   class.
3. It resumes rather than restarting. A container running fifteen years
   across a universe will be interrupted; a job that started over would
   never finish.

The provider is fake for the usual reason — Module 04 tests its own
fetchers against recorded payloads. What is under test is what this
process does with what comes back.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select

from data.provider_adapters.fmp.errors import FmpError
from data.provider_adapters.fmp.models import (
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    FetchProvenance,
)
from infra.db.schema.canonical import canonical_corporate_actions, canonical_ohlcv
from infra.deploy.backfill import (
    BackfillNotConfigured,
    BackfillSettings,
    run_backfill,
)
from infra.deploy.migrate import upgrade_to_head
from packages.config.settings import AppConfig, DatabaseSettings, ProvidersSettings

START = date(2010, 1, 4)
END = date(2010, 1, 8)
FETCHED_AT = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
SPLIT_DATE = date(2010, 1, 6)


def _provenance(endpoint: str) -> FetchProvenance:
    return FetchProvenance(endpoint=endpoint, url_path=f"/stable/{endpoint}", fetched_at=FETCHED_AT)


class FakeHistorySource:
    """`backfill_daily_history` plus the two corporate-action endpoints.

    `backfill_daily_history` is reimplemented rather than stubbed because
    the contract under test is the *interaction*: it calls `on_records`
    per symbol, outside its own error handling, and the entrypoint's
    persistence hook has to survive that. A stub that returned a list
    would not exercise the thing that can go wrong.
    """

    def __init__(
        self,
        *,
        symbols: tuple[str, ...],
        already_complete: tuple[str, ...] = (),
        failing_actions: tuple[str, ...] = (),
    ) -> None:
        self.symbols = symbols
        self.already_complete = already_complete
        self.failing_actions = failing_actions
        self.price_ranges: list[tuple[str, date | None, date | None]] = []
        self.action_requests: list[tuple[str, str]] = []
        self.job_names: list[str] = []

    async def backfill_daily_history(
        self, symbols, *, job_name="", start=None, end=None, on_records=None
    ):
        self.job_names.append(job_name)
        pending = [s for s in symbols if s not in self.already_complete]

        report = _BackfillOutcome(
            attempted=len(pending),
            already_complete=len(symbols) - len(pending),
        )
        for symbol in pending:
            self.price_ranges.append((symbol, start, end))
            result = _Result(_bars(symbol, start, end))
            if on_records is not None:
                on_records(result)
            if not result.records:
                report.empty.append(symbol)
        return report

    async def fetch_splits(self, symbol: str):
        self.action_requests.append((symbol, "splits"))
        if symbol in self.failing_actions:
            raise FmpError("splits endpoint unavailable")
        return _Result(
            [
                CorporateAction(
                    provenance=_provenance("splits"),
                    symbol=symbol,
                    kind=CorporateActionKind.SPLIT,
                    event_date=SPLIT_DATE,
                    details={"numerator": 2, "denominator": 1},
                )
            ]
        )

    async def fetch_dividends(self, symbol: str):
        self.action_requests.append((symbol, "dividends"))
        if symbol in self.failing_actions:
            raise FmpError("dividends endpoint unavailable")
        return _Result(
            [
                CorporateAction(
                    provenance=_provenance("dividends"),
                    symbol=symbol,
                    kind=CorporateActionKind.DIVIDEND,
                    event_date=date(2010, 1, 7),
                    details={"dividend": "0.25", "declarationDate": "2009-12-20"},
                )
            ]
        )


class _BackfillOutcome:
    def __init__(self, *, attempted: int, already_complete: int) -> None:
        self.attempted = attempted
        self.already_complete = already_complete
        self.succeeded = attempted
        self.empty: list[str] = []
        self.failed: dict[str, str] = {}


class _Result:
    def __init__(self, records: list[Any]) -> None:
        self.records = records
        self.empty_reason = None


def _bars(symbol: str, start: date | None, end: date | None) -> list[DailyBar]:
    """One bar per business day in the requested range.

    Built from the range the caller actually asked for, so a test can
    assert the range was honoured by counting what came back rather than
    by inspecting the request.
    """
    import pandas as pd

    days = pd.bdate_range(start or START, end or END)
    return [
        DailyBar(
            provenance=_provenance("historical_price_eod_full"),
            symbol=symbol,
            bar_date=day.date(),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1_000_000,
        )
        for day in days
    ]


@pytest.fixture
def at_head(fresh_engine: Engine, alembic_target) -> Engine:
    upgrade_to_head(fresh_engine)
    return fresh_engine


@pytest.fixture
def registered(at_head: Engine):
    """Two securities, so per-symbol isolation has something to isolate."""
    from data.canonical_model.exchanges import CanonicalExchange
    from data.normalization.identity import SecurityIdentityResolver

    identities: dict[str, UUID] = {}
    with at_head.begin() as connection:
        resolver = SecurityIdentityResolver(connection)
        for symbol in ("AAA", "BBB"):
            identities[symbol] = resolver.register(
                symbol,
                exchange=CanonicalExchange.NASDAQ,
                valid_from=datetime(2000, 1, 1, tzinfo=UTC),
                name=f"{symbol} Corp.",
            )
    return identities


def _settings(**overrides) -> BackfillSettings:
    return BackfillSettings(
        start=overrides.pop("start", START),
        end=overrides.pop("end", END),
        symbols=overrides.pop("symbols", ()),
        include_actions=overrides.pop("include_actions", True),
    )


@pytest.fixture
def app_config(tmp_path) -> AppConfig:
    """Config built directly, never loaded from the environment.

    `get_config()` reads `.env` and requires a database block, so a test
    that let it do that would assert against whatever machine it ran on.
    `run_backfill` takes the config as a parameter for exactly this
    reason — the same shape `run_daily_ingestion` uses.
    """
    return AppConfig(
        database=DatabaseSettings(host="127.0.0.1", port=5432, name="argus_test", user="argus"),
        providers=ProvidersSettings(fmp_checkpoint_dir=str(tmp_path / "checkpoints")),
    )


def _run(engine: Engine, source, identities, settings=None, app_config=None):
    return asyncio.run(
        run_backfill(
            engine,
            source,
            settings=settings or _settings(),
            identities=identities,
            app_config=app_config,
        )
    )


def _counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as connection:
        bars = connection.execute(select(func.count()).select_from(canonical_ohlcv)).scalar_one()
        actions = connection.execute(
            select(func.count()).select_from(canonical_corporate_actions)
        ).scalar_one()
    return bars, actions


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_a_run_without_a_range_refuses_rather_than_guessing():
    """Fifteen years hardcoded would be a spending decision.

    How far back to fetch decides whether the result can produce a setup
    at all, and it costs real quota. This job is not entitled to choose
    it, so it says so instead.
    """
    with pytest.raises(BackfillNotConfigured, match="ARGUS_BACKFILL_START"):
        BackfillSettings.from_environment({})


def test_a_backwards_range_is_refused():
    """An empty range would fetch nothing and report success."""
    with pytest.raises(BackfillNotConfigured, match="before"):
        BackfillSettings.from_environment(
            {"ARGUS_BACKFILL_START": "2015-01-01", "ARGUS_BACKFILL_END": "2010-01-01"}
        )


def test_the_checkpoint_name_carries_the_range():
    """Two ranges are two jobs.

    Sharing a checkpoint would make a second run believe the first had
    already covered its symbols and skip every one — a silent no-op that
    looks like a fast success.
    """
    first = BackfillSettings.from_environment(
        {"ARGUS_BACKFILL_START": "2010-01-01", "ARGUS_BACKFILL_END": "2015-01-01"}
    )
    second = BackfillSettings.from_environment(
        {"ARGUS_BACKFILL_START": "2015-01-01", "ARGUS_BACKFILL_END": "2020-01-01"}
    )

    assert first.job_name() != second.job_name()


def test_actions_are_included_unless_explicitly_turned_off():
    """The default is the one that avoids G2's failure over fifteen years."""
    assert BackfillSettings.from_environment({"ARGUS_BACKFILL_START": "2010-01-01"}).include_actions
    off = BackfillSettings.from_environment(
        {"ARGUS_BACKFILL_START": "2010-01-01", "ARGUS_BACKFILL_ACTIONS": "0"}
    )
    assert off.include_actions is False


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def test_the_requested_range_is_what_gets_fetched_and_stored(
    at_head: Engine, registered, app_config
):
    """The whole point: history, over a range the caller chose."""
    source = FakeHistorySource(symbols=("AAA", "BBB"))

    report = _run(at_head, source, registered, app_config=app_config)

    assert {symbol for symbol, _s, _e in source.price_ranges} == {"AAA", "BBB"}
    assert all(start == START and end == END for _s, start, end in source.price_ranges)
    assert report.bars_inserted > 0
    assert report.securities_written == 2

    bars, _actions = _counts(at_head)
    assert bars == report.bars_inserted


def test_corporate_actions_are_fetched_and_stored_alongside_the_bars(
    at_head: Engine, registered, app_config
):
    """G2 over fifteen years, prevented rather than repeated.

    Without this, every split in the backfilled history is an
    uncorrected discontinuity and Module 15 records each as a
    catastrophic failure of a setup that succeeded.
    """
    source = FakeHistorySource(symbols=("AAA", "BBB"))

    report = _run(at_head, source, registered, app_config=app_config)

    assert sorted(source.action_requests) == [
        ("AAA", "dividends"),
        ("AAA", "splits"),
        ("BBB", "dividends"),
        ("BBB", "splits"),
    ]
    assert report.actions_inserted == 4

    _bars_count, actions = _counts(at_head)
    assert actions == 4


def test_turning_actions_off_costs_no_requests(at_head: Engine, registered, app_config):
    """The switch exists; it is simply not the default."""
    source = FakeHistorySource(symbols=("AAA", "BBB"))

    report = _run(
        at_head, source, registered, _settings(include_actions=False), app_config=app_config
    )

    assert source.action_requests == []
    assert report.actions_inserted == 0
    assert report.bars_inserted > 0


def test_the_cost_is_estimated_before_the_run(at_head: Engine, registered, app_config):
    """An operator about to spend a day of quota should see the number.

    Three requests per symbol with actions, one without — derived from
    the same constants the run uses, so the estimate cannot drift from
    the behaviour.
    """
    source = FakeHistorySource(symbols=("AAA", "BBB"))

    with_actions = _run(at_head, source, registered, app_config=app_config)
    assert with_actions.estimated_requests == 2 * 3

    without = _run(
        at_head,
        FakeHistorySource(symbols=("AAA", "BBB")),
        registered,
        _settings(include_actions=False),
        app_config=app_config,
    )
    assert without.estimated_requests == 2 * 1


def test_symbols_already_checkpointed_are_not_refetched(at_head: Engine, registered, app_config):
    """Resumability, which is what makes a fifteen-year job finishable.

    A container running that job will be restarted. Starting over each
    time means never finishing.
    """
    source = FakeHistorySource(symbols=("AAA", "BBB"), already_complete=("AAA",))

    report = _run(at_head, source, registered, app_config=app_config)

    assert report.already_checkpointed == 1
    assert {symbol for symbol, _s, _e in source.price_ranges} == {"BBB"}


def test_a_failing_actions_endpoint_does_not_cost_the_prices(
    at_head: Engine, registered, app_config
):
    """Per-symbol isolation, and the right direction to fail in.

    The bars are already written and are worth having even unadjusted —
    what must not happen is losing them, or losing the other symbol,
    because one endpoint was down. The failure is named in the report so
    the gap is visible rather than assumed.
    """
    source = FakeHistorySource(symbols=("AAA", "BBB"), failing_actions=("AAA",))

    report = _run(at_head, source, registered, app_config=app_config)

    assert "AAA:actions" in report.failed
    assert report.bars_inserted > 0
    # BBB's actions still landed.
    assert report.actions_inserted == 2
    assert report.healthy


def test_a_run_over_a_universe_of_nobody_is_not_healthy(at_head: Engine, app_config):
    """Zero securities is a configuration problem, not a fast success."""
    report = _run(at_head, FakeHistorySource(symbols=()), {}, app_config=app_config)

    assert report.securities == 0
    assert report.bars_inserted == 0
    assert report.healthy is False


def test_a_second_identical_run_writes_no_duplicates(at_head: Engine, registered, app_config):
    """The canonical tables are append-only and the writers are insert-only.

    Re-running a backfill after an interruption re-offers rows that are
    already stored, and it has to be free rather than a duplicate or an
    error.
    """
    _run(at_head, FakeHistorySource(symbols=("AAA", "BBB")), registered, app_config=app_config)
    before = _counts(at_head)

    second = _run(
        at_head, FakeHistorySource(symbols=("AAA", "BBB")), registered, app_config=app_config
    )

    assert second.bars_inserted == 0
    assert second.actions_inserted == 0
    assert _counts(at_head) == before
