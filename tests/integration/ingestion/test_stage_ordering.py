"""One healthy run must fill every table it is supposed to fill.

This file exists because of a bug that made three of the four ingestion
stages permanently no-ops, and it is written to fail loudly if the shape
that caused it ever comes back.

## The bug

`refresh_due_securities` writes a `deep_refresh_log` row for every
security it refreshes, stamped with the run's own trading date. The
ownership and Terminal-data stages then asked "who is due today?" — a
question answered from that same log, through `tiers.decide`:

```python
if last.refreshed_on >= target_date:
    return DueDecision(due=False, trigger=ALREADY_REFRESHED, ...)
```

So on any run where the deep refresh succeeded, both later stages saw an
empty due list. `insider_trades`, `institutional_ownership`,
`canonical_disclosures`, `canonical_snapshots`, `analyst_grades` and
`technical_indicators` were never written on a healthy run — which is
every table the Ultimate plan is bought for.

The stages were correct; the *order* was wrong. The due list is now
computed once, before the deep refresh, and the same list is handed to
all three — which also matches what the tier decision was always for:
three stages on one cadence should share one decision, not ask three
times and get different answers depending on who ran first.

## Why the existing tests could not catch it

Two independent reasons, and both are worth knowing because they are the
general lesson rather than a detail of this bug:

1. `FakeFetcher` had none of the Ultimate methods, so the capability
   guards marked both stages "skipped" and the empty due list was never
   reached. A test double that cannot do the thing under test will
   happily agree that the thing works.
2. `test_ownership_ingestion.py` and `test_terminal_data.py` call
   `ingest_ownership` and `ingest_terminal_data` directly with a
   hand-built `due=[member]`. That is the right shape for testing those
   functions and it steps straight over the part that was broken.

So the assertion below is deliberately made at the **orchestrator**
level, against a fetcher that really can serve the endpoints. Nothing
short of that would have seen it.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import Engine, func, select

from core.ingestion.config import IngestionConfig
from core.ingestion.orchestrator import run_daily_ingestion
from infra.db.enums import MarketState
from infra.db.schema.ownership_signals import insider_trades, institutional_ownership
from infra.db.schema.sec_filings import sec_filings
from infra.db.schema.terminal_data import (
    analyst_grades,
    canonical_disclosures,
    canonical_snapshots,
    technical_indicators,
)
from tests.integration.ingestion.conftest import (
    NOW,
    TARGET_DATE,
    FakeFetcher,
    UltimateFakeFetcher,
    article,
    statement,
)

#: Every table a healthy run must write for a security that is due. The
#: list is the point: an assertion per table would let a future stage be
#: added and silently not covered.
ULTIMATE_TABLES = (
    insider_trades,
    institutional_ownership,
    canonical_disclosures,
    canonical_snapshots,
    analyst_grades,
    technical_indicators,
)


def _ultimate_fetcher(symbols, checkpoint_dir) -> UltimateFakeFetcher:
    known = set(symbols)
    return UltimateFakeFetcher(
        bars_for=lambda s: [] if s not in known else _bars(s),
        statements_for=lambda s, k: [statement(s, k)] if s in known else [],
        news_for=lambda s: [article(s)] if s in known else [],
        filing_symbols=tuple(symbols),
        checkpoint_dir=checkpoint_dir,
    )


def _bars(symbol: str):
    from tests.integration.ingestion.conftest import session_bars

    return session_bars(symbol, (TARGET_DATE,))


def _run(engine, fetcher, version_id, app_config):
    return asyncio.run(
        run_daily_ingestion(
            engine,
            fetcher,
            universe_version_id=version_id,
            config=IngestionConfig(),
            app_config=app_config,
            now=NOW,
        )
    )


def _rows(engine: Engine, table, security_id) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(table).where(table.c.security_id == security_id)
        ).scalar_one()


def test_one_healthy_run_writes_every_ultimate_table(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The regression test for the stage-ordering bug.

    A security in UPTREND is due for everything. One run, and every
    Ultimate-plan table must hold at least one row for it. Before the fix
    all six were empty and the run reported success.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())
    security_id = identities["AAA"]

    report = _run(
        committing_engine, _ultimate_fetcher(symbols, checkpoint_dir), version_id, app_config
    )

    empty = [
        table.name for table in ULTIMATE_TABLES if _rows(committing_engine, table, security_id) == 0
    ]
    assert empty == [], f"a healthy run left these tables empty: {empty}"

    # And the run has to say it did the work, not merely have done it —
    # an operator reads the report, not the tables.
    assert report.ownership.insider_written > 0
    assert report.ownership.institutional_written > 0
    assert report.terminal_data.disclosures_written > 0
    assert report.terminal_data.snapshots_written > 0
    assert report.terminal_data.grades_written > 0
    assert report.terminal_data.indicator_points_written > 0


def test_the_later_stages_are_not_skipped_and_not_empty(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """Two ways a stage can do nothing, and neither is acceptable here.

    `skipped_reason` means the provider could not serve the endpoints —
    the honest state before an Ultimate key exists. An empty due list
    with no reason is the bug: the stage ran, asked who was due, and was
    told nobody. Distinguishing them is the whole point of
    `skipped_reason` being a separate field from a zero count.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())

    report = _run(
        committing_engine, _ultimate_fetcher(symbols, checkpoint_dir), version_id, app_config
    )

    assert report.ownership.skipped_reason is None
    assert report.terminal_data.skipped_reason is None
    assert report.ownership.considered == 1
    assert report.terminal_data.considered == 1


def test_the_deep_refresh_still_runs_and_still_logs(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """Moving the decision earlier must not cost the stage it came from.

    The due list is now computed before the deep refresh rather than
    after it, so the obvious way to get this wrong is for the deep
    refresh itself to stop seeing anyone.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())

    report = _run(
        committing_engine, _ultimate_fetcher(symbols, checkpoint_dir), version_id, app_config
    )

    assert report.deep_refresh.due == 1
    assert report.deep_refresh.refreshed == 1
    assert report.deep_refresh.fundamentals_inserted > 0


def test_a_provider_without_the_plan_is_still_skipped_cleanly(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """Today's ARGUS, and it must stay a clean skip rather than a crash.

    The fix makes the due list non-empty, which means the capability
    guard is now the only thing standing between a lesser plan and a
    stream of `AttributeError`s. This asserts it still stands.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())
    known = set(symbols)

    plain = FakeFetcher(
        bars_for=lambda s: _bars(s) if s in known else [],
        statements_for=lambda s, k: [statement(s, k)] if s in known else [],
        news_for=lambda s: [article(s)] if s in known else [],
        checkpoint_dir=checkpoint_dir,
    )

    report = _run(committing_engine, plain, version_id, app_config)

    assert report.ownership.skipped_reason is not None
    assert report.terminal_data.skipped_reason is not None
    # The rest of the run is unaffected — that is what "skipped" means.
    assert report.deep_refresh.refreshed == 1
    assert report.prices.securities_written == 1


def test_a_security_on_no_watchlist_costs_no_ultimate_requests(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The due list still gates spending, which is why it exists.

    Making the list non-empty for due securities must not make it
    non-empty for everyone — that would be the opposite mistake, and an
    expensive one at 3,000 requests a minute.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UNCLASSIFIED})
    symbols = list(tickers_for(identities).values())
    fetcher = _ultimate_fetcher(symbols, checkpoint_dir)

    _run(committing_engine, fetcher, version_id, app_config)

    assert fetcher.insider_requests == []
    assert fetcher.institutional_requests == []
    assert fetcher.terminal_requests == []
    # The market-wide 8-K feed is not per-security and is not gated by
    # the tier: one request covers everyone, due or not.
    assert fetcher.filing_requests > 0


def test_the_bulk_filing_feed_still_lands_for_universe_members(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """8-K is the one Ultimate path that never used the due list."""
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())

    _run(committing_engine, _ultimate_fetcher(symbols, checkpoint_dir), version_id, app_config)

    assert _rows(committing_engine, sec_filings, identities["AAA"]) > 0
