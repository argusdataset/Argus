"""The tiered deep refresh against a real database and a real market state.

The unit tests in `tests/unit/ingestion/test_tier_dueness.py` prove the
decision. These prove the decision is reached from stored state — the
phase read live from the `market_state` projection, the last refresh read
from the log — and that the log row records the phase that drove it
rather than a phase anyone could mistake for a current one.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import Engine, select, update

from core.ingestion.config import IngestionConfig
from core.ingestion.orchestrator import run_daily_ingestion
from core.ingestion.tiers import INTERVAL_ELAPSED, NEVER_REFRESHED, TIER_ESCALATION
from infra.db.enums import MarketState
from infra.db.schema.ingestion import deep_refresh_log
from infra.db.schema.intelligence import market_state
from tests.integration.ingestion.conftest import (
    NOW,
    TARGET_DATE,
    FakeFetcher,
    article,
    session_bars,
    statement,
)


def _fetcher(symbols, checkpoint_dir) -> FakeFetcher:
    known = set(symbols)
    return FakeFetcher(
        bars_for=lambda s: session_bars(s, (TARGET_DATE,)) if s in known else [],
        statements_for=lambda s, k: [statement(s, k)] if s in known else [],
        news_for=lambda s: [article(s)] if s in known else [],
        checkpoint_dir=checkpoint_dir,
    )


def _run(engine, fetcher, version_id, app_config, *, now=NOW):
    return asyncio.run(
        run_daily_ingestion(
            engine,
            fetcher,
            universe_version_id=version_id,
            config=IngestionConfig(),
            app_config=app_config,
            now=now,
        )
    )


def _log_rows(engine: Engine, security_id):
    with engine.connect() as conn:
        return conn.execute(
            select(
                deep_refresh_log.c.refreshed_on,
                deep_refresh_log.c.triggering_watchlist,
                deep_refresh_log.c.market_state,
                deep_refresh_log.c.trigger,
                deep_refresh_log.c.config_version_label,
                deep_refresh_log.c.statements_written,
                deep_refresh_log.c.news_written,
            )
            .where(deep_refresh_log.c.security_id == security_id)
            .order_by(deep_refresh_log.c.refreshed_on)
        ).all()


@pytest.mark.parametrize(
    ("state", "watchlist"),
    [
        (MarketState.DOWN_TREND, "DOWN_TREND"),
        (MarketState.BASE_FORMING, "DOWN_TREND"),
        (MarketState.CONSOLIDATION, "CONSOLIDATION"),
        (MarketState.ACCUMULATION, "CONSOLIDATION"),
        (MarketState.BREAKOUT_WATCH, "BREAKOUT_READY"),
        (MarketState.BREAKOUT_READY, "BREAKOUT_READY"),
        (MarketState.UPTREND, "UPTREND"),
    ],
)
def test_the_log_records_the_watchlist_the_state_actually_maps_to(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config, state, watchlist
):
    """Every state on a list, mapped through Module 10's own `WATCHLISTS`.

    Parameterized over all seven rather than one example, because the
    map is the thing being trusted: a state quietly falling off a list
    would mean those securities never refresh, and nothing else would
    notice.
    """
    version_id, identities = universe(("AAA",), states={"AAA": state})
    symbols = list(tickers_for(identities).values())

    _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config)

    rows = _log_rows(committing_engine, identities["AAA"])
    assert len(rows) == 1
    assert rows[0].triggering_watchlist == watchlist
    assert rows[0].market_state == state.value
    assert rows[0].trigger == NEVER_REFRESHED
    assert rows[0].refreshed_on == TARGET_DATE


@pytest.mark.parametrize("state", [MarketState.UNCLASSIFIED, MarketState.DISTRIBUTION])
def test_a_security_on_no_watchlist_is_never_refreshed(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config, state
):
    """The two internal states, and the same rule Module 10 gives for them.

    Its bars are still fetched — OHLCV is unconditional — which is the
    point of the two scopes being separate.
    """
    version_id, identities = universe(("AAA",), states={"AAA": state})
    symbols = list(tickers_for(identities).values())
    fetcher = _fetcher(symbols, checkpoint_dir)

    report = _run(committing_engine, fetcher, version_id, app_config)

    assert _log_rows(committing_engine, identities["AAA"]) == []
    assert fetcher.statement_requests == []
    assert fetcher.news_requests == []
    assert report.prices.securities_written == 1


def test_a_security_with_no_state_row_at_all_is_never_refreshed(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """Absent is not a phase, and must not default into the cheapest tier."""
    version_id, identities = universe(("AAA",))
    symbols = list(tickers_for(identities).values())
    fetcher = _fetcher(symbols, checkpoint_dir)

    _run(committing_engine, fetcher, version_id, app_config)

    assert _log_rows(committing_engine, identities["AAA"]) == []
    assert fetcher.news_requests == []


def test_a_promotion_between_runs_forces_an_immediate_refresh(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The phase-transition reset, end to end.

    A security refreshed while in DOWN_TREND has twenty-nine days left
    on that tier. Moving it to CONSOLIDATION and running again the next
    day must refresh it, and the log must say *why* — a run whose
    decisions cannot be read back is a run nobody can debug.

    The second run's date is moved forward by a day rather than the
    clock being left alone, because the same-day rule would otherwise
    (correctly) refuse the second refresh.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.DOWN_TREND})
    symbols = list(tickers_for(identities).values())
    security_id = identities["AAA"]

    _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config)

    with committing_engine.begin() as conn:
        conn.execute(
            update(market_state)
            .where(market_state.c.security_id == security_id)
            .values(state=MarketState.CONSOLIDATION.value)
        )

    tomorrow = NOW.replace(day=NOW.day + 1)
    report = _run(
        committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config, now=tomorrow
    )

    rows = _log_rows(committing_engine, security_id)
    assert len(rows) == 2
    assert [row.trigger for row in rows] == [NEVER_REFRESHED, TIER_ESCALATION]
    assert [row.triggering_watchlist for row in rows] == ["DOWN_TREND", "CONSOLIDATION"]
    assert report.deep_refresh.triggers == {TIER_ESCALATION: 1}


def test_a_down_trend_security_is_not_refreshed_again_the_next_day(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The other half of the same claim: without a promotion, it waits."""
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.DOWN_TREND})
    symbols = list(tickers_for(identities).values())

    _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config)

    tomorrow = NOW.replace(day=NOW.day + 1)
    second = _fetcher(symbols, checkpoint_dir)
    report = _run(committing_engine, second, version_id, app_config, now=tomorrow)

    assert len(_log_rows(committing_engine, identities["AAA"])) == 1
    assert second.statement_requests == []
    assert second.news_requests == []
    assert report.deep_refresh.due == 0


def test_an_uptrend_security_is_refreshed_again_the_next_day(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """Daily means daily, and the second row's trigger says which rule fired."""
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())

    _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config)
    tomorrow = NOW.replace(day=NOW.day + 1)
    _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config, now=tomorrow)

    rows = _log_rows(committing_engine, identities["AAA"])
    assert len(rows) == 2
    assert [row.trigger for row in rows] == [NEVER_REFRESHED, INTERVAL_ELAPSED]


def test_the_log_row_records_which_policy_was_in_force(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The tier intervals can change. A row from before says so."""
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())

    _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config)

    rows = _log_rows(committing_engine, identities["AAA"])
    assert rows[0].config_version_label == IngestionConfig().version_label()
    assert rows[0].statements_written == len(IngestionConfig().statement_types)
    assert rows[0].news_written == 1


def test_both_phases_draw_on_one_provider_client(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """One set of token buckets, not two independent budgets.

    `FmpClient` owns the rate limiter, so two clients would be two
    ceilings against a per-minute limit FMP enforces once. The
    orchestrator takes a single source and passes the same object to
    both phases — asserted here by observing that one recorder saw both
    kinds of request.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())
    fetcher = _fetcher(symbols, checkpoint_dir)

    _run(committing_engine, fetcher, version_id, app_config)

    assert fetcher.price_requests
    assert fetcher.statement_requests
    assert fetcher.news_requests
