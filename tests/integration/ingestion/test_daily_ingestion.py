"""One day's ingestion, end to end against a real database.

The tests named in the module's brief, in the order they matter:

- a trading day with no prior data gets bars for the whole universe;
- a re-run of the same day is a no-op — no duplicate rows, and *no
  doubled FMP requests*, which is the half that costs money;
- a run interrupted mid-universe resumes from the checkpoint;
- fundamentals persisted through this path carry an honest
  `availability_time` derived from FMP's `acceptedDate`.

The provider is a recording fake — see the conftest on why — so "no
doubled requests" is an assertion about a counter rather than a claim.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import Engine, func, select

from core.ingestion.config import IngestionConfig
from core.ingestion.orchestrator import run_daily_ingestion
from data.canonical_model.pit import DEFAULT_LAG_POLICY
from data.canonical_model.records import CanonicalTimeframe
from infra.db.enums import MarketState
from infra.db.schema.canonical import canonical_fundamentals, canonical_ohlcv
from infra.db.schema.news import canonical_news
from tests.integration.ingestion.conftest import (
    NOW,
    TARGET_DATE,
    FakeFetcher,
    InterruptedIngestion,
    article,
    close_of,
    session_bars,
    statement,
)

TICKERS = ("AAA", "BBB", "CCC")
#: The three sessions the fake provider returns for each symbol — the
#: target date and the two before it, which is what a lookback window
#: covering a missed run would actually bring back.
SESSIONS = (
    TARGET_DATE,
    TARGET_DATE.fromordinal(TARGET_DATE.toordinal() - 3),
    TARGET_DATE.fromordinal(TARGET_DATE.toordinal() - 4),
)


def _fetcher(identities, tickers_for, checkpoint_dir, **kwargs) -> FakeFetcher:
    symbols = set(tickers_for(identities).values())
    return FakeFetcher(
        bars_for=lambda symbol: session_bars(symbol, SESSIONS) if symbol in symbols else [],
        statements_for=lambda symbol, kind: [statement(symbol, kind)] if symbol in symbols else [],
        news_for=lambda symbol: [article(symbol)] if symbol in symbols else [],
        checkpoint_dir=checkpoint_dir,
        **kwargs,
    )


def _run(engine: Engine, fetcher: FakeFetcher, version_id, app_config, **kwargs):
    return asyncio.run(
        run_daily_ingestion(
            engine,
            fetcher,
            universe_version_id=version_id,
            config=IngestionConfig(),
            app_config=app_config,
            now=NOW,
            **kwargs,
        )
    )


def _bar_count(engine: Engine, security_ids) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(func.count())
            .select_from(canonical_ohlcv)
            .where(
                canonical_ohlcv.c.security_id.in_(list(security_ids)),
                canonical_ohlcv.c.timeframe == CanonicalTimeframe.DAILY.value,
            )
        ).scalar_one()


@pytest.fixture
def prepared(universe, tickers_for, checkpoint_dir):
    """A three-name universe, all in DOWN_TREND, with a fake provider."""
    version_id, identities = universe(
        TICKERS, states=dict.fromkeys(TICKERS, MarketState.DOWN_TREND)
    )
    fetcher = _fetcher(identities, tickers_for, checkpoint_dir)
    return version_id, identities, fetcher


def test_a_day_with_no_prior_data_gets_bars_for_the_whole_universe(
    committing_engine, prepared, app_config
):
    version_id, identities, fetcher = prepared

    report = _run(committing_engine, fetcher, version_id, app_config)

    assert report.trading_date == TARGET_DATE
    assert report.prices.universe_size == len(TICKERS)
    assert report.prices.securities_written == len(TICKERS)
    assert sorted(fetcher.price_requests) == sorted(set(fetcher.price_requests))
    assert len(fetcher.price_requests) == len(TICKERS)
    assert _bar_count(committing_engine, identities.values()) == len(TICKERS) * len(SESSIONS)


def test_the_target_session_is_the_one_the_scanner_will_ask_about(
    committing_engine, prepared, app_config
):
    """Not "today". Module 18 decides which session is due, and this reuses it.

    At 21:00 UTC on the Tuesday the session due to be scanned is the
    Monday, because Tuesday's cutoff is its close plus the scan offset
    and has not arrived. If this module picked its own date the two could
    disagree, and the disagreement would look exactly like a provider
    delay.
    """
    version_id, _, fetcher = prepared

    report = _run(committing_engine, fetcher, version_id, app_config)

    assert report.trading_date == TARGET_DATE
    assert report.trading_date < NOW.date()


def test_a_re_run_of_the_same_day_writes_nothing_and_asks_for_nothing(
    committing_engine, prepared, app_config
):
    """The database, not the checkpoint, is what makes this true.

    A Railway cron container starts each firing with an empty
    filesystem, so the checkpoint from the previous run is simply not
    there. The skip that survives that is "which members already hold a
    bar for this session", which is why it is asked before any fetching.

    Proven by clearing the checkpoint directory between the two runs:
    the second run still requests nothing.
    """
    version_id, identities, fetcher = prepared

    first = _run(committing_engine, fetcher, version_id, app_config)
    after_first = _bar_count(committing_engine, identities.values())
    requests_after_first = fetcher.total_requests

    for path in fetcher._checkpoint_dir.glob("*.jsonl"):  # noqa: SLF001 - the point of the test
        path.unlink()

    second = _run(committing_engine, fetcher, version_id, app_config)

    assert second.prices.already_held == len(TICKERS)
    assert second.prices.attempted == 0
    assert fetcher.total_requests == requests_after_first
    assert _bar_count(committing_engine, identities.values()) == after_first
    assert first.prices.securities_written == len(TICKERS)


def test_an_interrupted_run_resumes_from_the_checkpoint(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """Module 04's JSONL checkpoint, reused rather than reinvented.

    Isolating the checkpoint from the database skip takes a little care,
    because the two normally cover each other. The first symbol here
    returns *no* bars — a real case: a halted or newly-delisted name the
    provider has nothing for — so it is checkpointed as fetched while
    leaving nothing in `canonical_ohlcv`. The database skip therefore
    cannot account for it, and the second run skipping it anyway is the
    checkpoint doing its job.

    The first run then dies partway, as a killed container would.
    """
    version_id, identities = universe(
        TICKERS, states=dict.fromkeys(TICKERS, MarketState.DOWN_TREND)
    )
    symbols = sorted(tickers_for(identities).values())
    empty, *rest = symbols

    def _bars(symbol: str):
        if symbol == empty:
            return []
        return session_bars(symbol, SESSIONS) if symbol in rest else []

    dying = FakeFetcher(bars_for=_bars, checkpoint_dir=checkpoint_dir, fail_after=1)
    with pytest.raises(InterruptedIngestion):
        _run(committing_engine, dying, version_id, app_config)

    assert dying.price_requests == [empty]

    resuming = FakeFetcher(bars_for=_bars, checkpoint_dir=checkpoint_dir)
    report = _run(committing_engine, resuming, version_id, app_config)

    assert empty not in resuming.price_requests
    assert sorted(resuming.price_requests) == rest
    assert report.prices.already_checkpointed == 1


def test_fundamentals_carry_the_accepted_date_not_today(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The PIT rule that makes a fundamental usable in a backtest.

    Module 05 derives `observation_time` from FMP's `acceptedDate` — the
    instant the filing became public — and refuses the row outright
    rather than falling back to the fiscal period end. This asserts the
    value survives this module's path, because a fundamental stamped
    "available today" would let a replay read a filing weeks before it
    existed and nothing would fail.
    """
    version_id, identities = universe(("DDD",), states={"DDD": MarketState.UPTREND})
    fetcher = _fetcher(identities, tickers_for, checkpoint_dir)
    accepted = statement("X", "INCOME_STATEMENT").accepted_date

    _run(committing_engine, fetcher, version_id, app_config)

    security_id = identities["DDD"]
    with committing_engine.connect() as conn:
        rows = conn.execute(
            select(
                canonical_fundamentals.c.observation_time,
                canonical_fundamentals.c.availability_time,
                canonical_fundamentals.c.event_time,
            ).where(canonical_fundamentals.c.security_id == security_id)
        ).all()

    assert rows
    for row in rows:
        assert row.observation_time == accepted
        assert row.availability_time == accepted + DEFAULT_LAG_POLICY.fundamentals
        # The fiscal period end, which is always available and always the
        # wrong source for observability.
        assert row.event_time < row.observation_time


def test_news_is_written_once_however_many_runs_see_it(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """`canonical_news` has two *partial* unique indexes, not one constraint.

    Module 19 built it that way so a provider omitting URLs cannot
    collapse every article onto a single NULL. Postgres infers a partial
    index only when a statement repeats its predicate, so this module
    inserts URL-bearing and URL-less articles separately — and a bug
    there shows up as either a duplicate row or a raised constraint
    violation, both of which this catches.
    """
    version_id, identities = universe(("EEE",), states={"EEE": MarketState.UPTREND})
    symbol = tickers_for(identities)[identities["EEE"]]
    fetcher = FakeFetcher(
        bars_for=lambda s: session_bars(s, SESSIONS) if s == symbol else [],
        news_for=lambda s: [article(s), article(s, url=None)] if s == symbol else [],
        checkpoint_dir=checkpoint_dir,
    )

    _run(committing_engine, fetcher, version_id, app_config)
    first = _news_count(committing_engine, identities["EEE"])

    # A second refresh of the same security on a later date, same articles.
    with committing_engine.begin() as conn:
        from infra.db.schema.ingestion import deep_refresh_log

        conn.execute(
            deep_refresh_log.delete().where(deep_refresh_log.c.security_id == identities["EEE"])
        )
    _run(committing_engine, fetcher, version_id, app_config)

    assert first == 2
    assert _news_count(committing_engine, identities["EEE"]) == first


def _news_count(engine: Engine, security_id) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(func.count())
            .select_from(canonical_news)
            .where(canonical_news.c.security_id == security_id)
        ).scalar_one()


def test_bars_land_on_the_session_close_not_the_fetch_time(committing_engine, prepared, app_config):
    """`event_time` is the session close, which is what makes coverage work.

    Module 18's readiness query brackets the UTC day containing the
    close. A bar stamped with the moment it was fetched would fall
    outside that bracket on most days and inside it on some, which is
    the worst of both.
    """
    version_id, identities, fetcher = prepared

    _run(committing_engine, fetcher, version_id, app_config)

    with committing_engine.connect() as conn:
        stamps = (
            conn.execute(
                select(canonical_ohlcv.c.event_time)
                .where(canonical_ohlcv.c.security_id == identities["AAA"])
                .order_by(canonical_ohlcv.c.event_time)
            )
            .scalars()
            .all()
        )

    assert close_of(TARGET_DATE) in stamps
