"""Splits and dividends reach the database, and a split actually adjusts.

Issue G2 was not that the corporate-action pipeline was broken. Every
piece of it worked: `fetch_splits`, `translate_corporate_action`,
`write_corporate_actions`, and Module 08's read-time adjustment. Nothing
called the first one. The pipe was laid and no water went in.

So these tests come in two halves, and the second is the one that matters.

**The wiring**: the deep refresh now asks for splits and dividends on the
same tiered cadence it uses for fundamentals and news, and what comes
back lands in `canonical_corporate_actions`.

**The consequence**: a security with a 2-for-1 split, read back through
`load_panel`, has its pre-split prices halved. That is the assertion that
would have caught G2 — and it is stated in the terms Module 15's README
uses, because the cost of the bug was not an empty table, it was an
unadjusted split reading as a −50% single-bar collapse and a successful
setup being recorded as a catastrophic failure.

## Why this is the opposite of the isolation rules elsewhere

News-volume, insider and 13F signals are all fenced off from
`core/scoring` and `core/market_state` by structural tests. Corporate
actions are the reverse case and deliberately so: they must reach the
core price path, because that is the only way the adjusted series is
right. A test that asserted corporate actions *do not* influence the
feature engine would be asserting the bug.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select

from core.feature_engine.panel import load_panel
from core.ingestion.config import IngestionConfig
from core.ingestion.orchestrator import run_daily_ingestion
from data.canonical_model.records import CanonicalCorporateActionType
from infra.db.enums import MarketState
from infra.db.schema.canonical import canonical_corporate_actions
from infra.db.schema.ingestion import deep_refresh_log
from tests.integration.feature_engine.conftest import insert_bars
from tests.integration.ingestion.conftest import (
    NOW,
    TARGET_DATE,
    FakeFetcher,
    article,
    daily_bar,
    dividend,
    split,
    statement,
)

#: The split lands mid-history so there are bars on both sides of it.
SPLIT_DATE = date(2026, 2, 17)
#: 100 before a 2-for-1, 50 after. Raw, that is the −50% single bar
#: Module 15's README describes; adjusted, it must be a flat line.
PRE_SPLIT_CLOSE = 100.0
POST_SPLIT_CLOSE = 50.0
#: A dividend the provider declared two weeks before it went ex.
DIVIDEND_DATE = date(2026, 1, 20)
DECLARED_DATE = date(2026, 1, 6)


def _fetcher(symbols, checkpoint_dir, *, actions: bool = True) -> FakeFetcher:
    known = set(symbols)

    def actions_for(symbol: str, kind) -> list:
        if not actions or symbol not in known:
            return []
        if kind.value == "split":
            return [split(symbol, event_date=SPLIT_DATE, numerator=2, denominator=1)]
        return [dividend(symbol, event_date=DIVIDEND_DATE, declared_on=DECLARED_DATE)]

    return FakeFetcher(
        # The run date is after the split, so its bar carries the
        # post-split price. A pre-split price on a post-split date would
        # be a discontinuity the fixture invented, and the adjustment
        # test below would fail on the fixture rather than on the code.
        bars_for=lambda s: (
            [daily_bar(s, TARGET_DATE, close=POST_SPLIT_CLOSE)] if s in known else []
        ),
        statements_for=lambda s, k: [statement(s, k)] if s in known else [],
        news_for=lambda s: [article(s)] if s in known else [],
        actions_for=actions_for,
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


def _stored_actions(engine: Engine, security_id):
    with engine.connect() as conn:
        return conn.execute(
            select(
                canonical_corporate_actions.c.action_type,
                canonical_corporate_actions.c.effective_date,
                canonical_corporate_actions.c.details,
                canonical_corporate_actions.c.observation_time,
                canonical_corporate_actions.c.availability_time,
            )
            .where(canonical_corporate_actions.c.security_id == security_id)
            .order_by(canonical_corporate_actions.c.effective_date)
        ).all()


# --------------------------------------------------------------------------
# The wiring: the refresh asks, and what comes back is stored
# --------------------------------------------------------------------------


def test_a_due_security_has_its_splits_and_dividends_fetched(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """Two requests per security, and G2's core claim inverted.

    Before this change `fetch_splits` and `fetch_dividends` were called
    from nowhere in the ingestion at all. The assertion on
    `action_requests` is therefore the whole of the fix in one line.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())
    fetcher = _fetcher(symbols, checkpoint_dir)

    _run(committing_engine, fetcher, version_id, app_config)

    kinds = [kind.value for _symbol, kind in fetcher.action_requests]
    assert sorted(kinds) == ["dividend", "split"]
    assert {symbol for symbol, _kind in fetcher.action_requests} == set(symbols)


def test_fetched_actions_land_in_canonical_corporate_actions(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The table G2 said was permanently empty, no longer empty."""
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())

    report = _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config)

    rows = _stored_actions(committing_engine, identities["AAA"])
    assert len(rows) == 2
    assert {row.action_type for row in rows} == {
        CanonicalCorporateActionType.SPLIT.value,
        CanonicalCorporateActionType.DIVIDEND.value,
    }
    assert report.deep_refresh.corporate_actions_inserted == 2


def test_a_dividends_declaration_date_is_when_it_became_knowable(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """A declared dividend is public from the declaration, not the ex-date.

    Splits carry no announcement date from FMP and fall back to the
    effective date; dividends do carry one and must use it. Asserted
    because the two types take different paths through
    `translate_corporate_action` and only one of them is exercised by the
    adjustment test below.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())

    _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config)

    rows = {row.action_type: row for row in _stored_actions(committing_engine, identities["AAA"])}
    dividend_row = rows[CanonicalCorporateActionType.DIVIDEND.value]
    split_row = rows[CanonicalCorporateActionType.SPLIT.value]

    assert dividend_row.observation_time.date() == DECLARED_DATE
    # The split's observation is its effective date — conservative, and
    # `translate_corporate_action` explains why at length.
    assert split_row.observation_time.date() == SPLIT_DATE


def test_a_second_run_re_offers_the_same_actions_and_writes_nothing(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """Insert-only, so a re-fetch of unchanged history is a no-op.

    A security in UPTREND is refreshed daily and its whole split history
    comes back every time. Without `ON CONFLICT DO NOTHING` that would be
    a duplicate split per day, and a duplicated 2-for-1 would adjust the
    series by four.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())

    _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config)
    second = _run(
        committing_engine,
        _fetcher(symbols, checkpoint_dir),
        version_id,
        app_config,
        now=NOW + timedelta(days=1),
    )

    assert second.deep_refresh.corporate_actions_inserted == 0
    assert len(_stored_actions(committing_engine, identities["AAA"])) == 2


def test_the_refresh_log_records_what_the_actions_pass_saw(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """In `detail`, not a new column.

    `deep_refresh_log` counts statements and news in columns; a third
    would be a migration for a number the run report already carries. The
    detail payload keeps a past refresh answerable about what it fetched.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())

    _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config)

    with committing_engine.connect() as conn:
        detail = conn.execute(
            select(deep_refresh_log.c.detail).where(
                deep_refresh_log.c.security_id == identities["AAA"]
            )
        ).scalar_one()

    assert detail["corporate_actions_offered"] == 2
    assert detail["corporate_actions_written"] == 2


def test_a_security_that_is_not_due_costs_no_action_requests(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The tier is what makes this affordable.

    A security on no watchlist is never deep-refreshed, so it never costs
    the two extra requests. That is the whole argument for putting
    corporate actions here rather than in the daily price path — see the
    volume note in `core/ingestion/README.md`.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UNCLASSIFIED})
    symbols = list(tickers_for(identities).values())
    fetcher = _fetcher(symbols, checkpoint_dir)

    _run(committing_engine, fetcher, version_id, app_config)

    assert fetcher.action_requests == []
    assert _stored_actions(committing_engine, identities["AAA"]) == []
    # Its bars were still fetched — OHLCV is unconditional.
    assert fetcher.price_requests == symbols


def test_a_provider_error_on_splits_costs_the_whole_refresh(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """Deliberate, and the alternative is worse.

    Swallowing the error would still write the log row, and the log row
    is what says "refreshed". A DOWN_TREND security would then wait 30
    days before retrying a split it never fetched — which is exactly the
    silence G2 was about. Failing the refresh makes it due tomorrow.
    """
    from data.provider_adapters.fmp.errors import FmpError

    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())
    fetcher = _fetcher(symbols, checkpoint_dir)

    async def failing_splits(symbol: str):
        raise FmpError("splits endpoint unavailable")

    fetcher.fetch_splits = failing_splits  # type: ignore[method-assign]

    report = _run(committing_engine, fetcher, version_id, app_config)

    assert report.deep_refresh.failed
    # No log row, so tomorrow's run treats it as never refreshed.
    with committing_engine.connect() as conn:
        logged = conn.execute(
            select(deep_refresh_log.c.security_id).where(
                deep_refresh_log.c.security_id == identities["AAA"]
            )
        ).all()
    assert logged == []


# --------------------------------------------------------------------------
# The consequence: the split actually adjusts the series
# --------------------------------------------------------------------------


def test_an_ingested_split_halves_the_pre_split_prices(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """G2's failure scenario, run forwards and shown not to happen.

    A security trades at 100 before a 2-for-1 split and at 50 after. Raw,
    that is a −50% single bar — the excursion Module 15's README says
    records a successful setup as a catastrophic failure. Adjusted, the
    pre-split bars come back at 50 and the series is continuous.

    The split is written by the ingestion path under test, not by hand:
    the point is that the fetch reaches the table that `load_panel` reads.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())
    security_id = identities["AAA"]

    # Five sessions at 100 before the split, five at 50 after it. Raw,
    # this is the discontinuity; adjusted, it must be a flat line.
    with committing_engine.begin() as conn:
        insert_bars(
            conn,
            security_id,
            start=datetime(2026, 2, 10, tzinfo=UTC),
            closes=[PRE_SPLIT_CLOSE] * 5 + [POST_SPLIT_CLOSE] * 5,
        )

    _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config)

    with committing_engine.connect() as conn:
        panel = load_panel(conn, [security_id], NOW, max_lookback_bars=60)

    closes = panel.close_adj[security_id].dropna()
    pre_split = closes[closes.index < datetime(2026, 2, 17, tzinfo=UTC)]
    post_split = closes[closes.index >= datetime(2026, 2, 17, tzinfo=UTC)]

    assert not pre_split.empty and not post_split.empty
    # Every pre-split bar halved; every post-split bar untouched.
    assert all(abs(value - POST_SPLIT_CLOSE) < 1e-9 for value in pre_split)
    assert all(abs(value - POST_SPLIT_CLOSE) < 1e-9 for value in post_split)
    # And therefore no −50% bar anywhere in the adjusted series.
    returns = closes.pct_change().dropna()
    assert returns.min() > -0.01


def test_without_the_ingested_split_the_series_still_shows_the_false_crash(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The bug, reproduced, so the test above is known to be load-bearing.

    Same bars, same read, but the provider returns no corporate actions —
    which is precisely the state ARGUS was in before this fix, since
    nothing ever called the fetchers. The −50% bar appears.

    Without this test the one above could pass for the wrong reason: if
    `load_panel` were somehow adjusting from another source, or if the
    fixture prices were not actually discontinuous, nothing would say so.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())
    security_id = identities["AAA"]

    with committing_engine.begin() as conn:
        insert_bars(
            conn,
            security_id,
            start=datetime(2026, 2, 10, tzinfo=UTC),
            closes=[PRE_SPLIT_CLOSE] * 5 + [POST_SPLIT_CLOSE] * 5,
        )

    _run(
        committing_engine,
        _fetcher(symbols, checkpoint_dir, actions=False),
        version_id,
        app_config,
    )

    with committing_engine.connect() as conn:
        panel = load_panel(conn, [security_id], NOW, max_lookback_bars=60)

    returns = panel.close_adj[security_id].dropna().pct_change().dropna()
    assert returns.min() == pytest.approx(-0.5, abs=1e-9)


def test_a_split_is_not_applied_before_it_was_knowable(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """PIT survives the fix.

    `load_corporate_actions_as_of` filters on `availability_time`, so a
    replay of a date before the split took effect must see the unadjusted
    series — that is what a trader saw at the time. Closing G2 must not
    quietly turn the adjusted series into a source of foreknowledge.
    """
    version_id, identities = universe(("AAA",), states={"AAA": MarketState.UPTREND})
    symbols = list(tickers_for(identities).values())
    security_id = identities["AAA"]

    with committing_engine.begin() as conn:
        insert_bars(
            conn,
            security_id,
            start=datetime(2026, 2, 10, tzinfo=UTC),
            closes=[PRE_SPLIT_CLOSE] * 5 + [POST_SPLIT_CLOSE] * 5,
        )

    _run(committing_engine, _fetcher(symbols, checkpoint_dir), version_id, app_config)

    before_the_split = datetime(2026, 2, 13, 23, 0, tzinfo=UTC)
    with committing_engine.connect() as conn:
        panel = load_panel(conn, [security_id], before_the_split, max_lookback_bars=60)

    closes = panel.close_adj[security_id].dropna()
    assert not closes.empty
    # Unadjusted: the split was not knowable at this instant.
    assert all(
        abs(Decimal(str(value)) - Decimal(str(PRE_SPLIT_CLOSE))) < Decimal("0.01")
        for value in closes
    )
