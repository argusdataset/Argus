"""Does the scanner actually act on what this module writes? — not yet, and why.

Module 26 exists because Module 18's readiness check counts bars in
`canonical_ohlcv` and nothing was writing them. It now writes them. The
obvious test is "and therefore the scanner reports ready", and that test
**fails** — for a reason in neither module's code, which is exactly the
kind of thing that stays invisible until something asserts it.

## The arithmetic

Module 05 derives a daily bar's `availability_time` as its session close
plus `ProviderLagPolicy.daily_bar`, which is **sixteen hours**: FMP
publishes EOD data within hours, but consolidated values settle
overnight, so claiming same-session knowledge of a close would be
claiming more than is true.

Module 18's point-in-time cutoff for scanning that same session is its
close plus `scan_offset_hours`, which is **five**.

`readiness.check_readiness` filters on `availability_time <= as_of`.
Sixteen is not less than or equal to five, so a bar this module writes is
not *knowable* at the cutoff the scanner applies to it, and coverage
reads zero however complete the ingestion was. The scanner would record
`DATA_NOT_READY` forever — the same outcome as before this module
existed, reached a different way.

## Why the existing tests never caught it

Module 18's own integration fixtures insert bars with a one-hour
availability lag (`tests/integration/feature_engine/conftest.py`), which
is fifteen hours more optimistic than what Module 05 actually stamps on a
bar in production. Every readiness test passes against a bar no
production path can produce.

## Whose number it is

Module 18's. `scan_offset_hours` must exceed the bar-availability lag or
the scanner can never see a bar for the session it is scanning. Module 26
was told not to modify `core/live_scanner/`, so this is flagged rather
than fixed — but the last test below shows the one-number change working,
so the fix is demonstrated rather than merely asserted to exist.

The tag on that number deserves a second look too: `scan_offset_hours` is
labelled `operational`, whose stated meaning is "bounds how the
computation runs, never what it produces". It feeds `as_of`, and `as_of`
decides what a scan can see. That is not operational.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from core.ingestion.config import IngestionConfig
from core.ingestion.orchestrator import run_daily_ingestion
from core.live_scanner.config import ScannerConfig, ScannerSettings
from core.live_scanner.readiness import check_readiness
from core.live_scanner.schedule import as_of_for
from data.canonical_model.pit import DEFAULT_LAG_POLICY, session_close
from data.canonical_model.records import CanonicalTimeframe
from infra.db.enums import MarketState
from infra.db.schema.canonical import canonical_ohlcv
from tests.integration.ingestion.conftest import (
    NOW,
    TARGET_DATE,
    FakeFetcher,
    session_bars,
)

TICKERS = ("AAA", "BBB", "CCC", "DDD", "EEE")


def _ingest(engine, universe, tickers_for, checkpoint_dir, app_config):
    version_id, identities = universe(
        TICKERS, states=dict.fromkeys(TICKERS, MarketState.UNCLASSIFIED)
    )
    known = set(tickers_for(identities).values())
    fetcher = FakeFetcher(
        bars_for=lambda s: session_bars(s, (TARGET_DATE,)) if s in known else [],
        checkpoint_dir=checkpoint_dir,
    )
    report = asyncio.run(
        run_daily_ingestion(
            engine,
            fetcher,
            universe_version_id=version_id,
            config=IngestionConfig(),
            app_config=app_config,
            now=NOW,
        )
    )
    return version_id, identities, report


def test_the_bars_are_written_for_every_universe_member(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The half that works: a day with no prior data gets full coverage."""
    _, identities, report = _ingest(
        committing_engine, universe, tickers_for, checkpoint_dir, app_config
    )

    with committing_engine.connect() as conn:
        stored = conn.execute(
            select(func.count(func.distinct(canonical_ohlcv.c.security_id))).where(
                canonical_ohlcv.c.security_id.in_(list(identities.values())),
                canonical_ohlcv.c.timeframe == CanonicalTimeframe.DAILY.value,
                canonical_ohlcv.c.event_time == session_close(TARGET_DATE),
            )
        ).scalar_one()

    assert stored == len(TICKERS)
    assert report.prices.securities_written == len(TICKERS)


def test_the_bar_availability_lag_exceeds_the_scanners_cutoff_offset():
    """The finding, as two numbers rather than a story.

    Nothing about this test needs a database or an ingestion run. It is
    here because it is the *cause* of the test below, and reading them
    next to each other is the point.
    """
    lag = DEFAULT_LAG_POLICY.daily_bar
    offset = ScannerConfig().settings.scan_offset

    assert lag > offset
    assert lag == timedelta(hours=16)
    assert offset == timedelta(hours=5)


def test_the_scanner_still_cannot_see_the_session_at_its_own_cutoff(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """A complete ingestion, and readiness still reports zero coverage.

    This is the honest answer to "does Module 18 stop being a permanent
    no-op". Today it does not, and the reason is the two numbers above.
    When Module 18's offset is raised past the bar lag this test must be
    inverted — and it should be, deliberately, by whoever makes that
    change.
    """
    version_id, _, _ = _ingest(committing_engine, universe, tickers_for, checkpoint_dir, app_config)

    with committing_engine.connect() as conn:
        readiness = check_readiness(
            conn,
            scan_date=TARGET_DATE,
            as_of=as_of_for(TARGET_DATE),
            universe_version_id=version_id,
        )

    assert readiness.universe_size == len(TICKERS)
    assert readiness.delivered == 0
    assert not readiness.ready


def test_the_same_bars_are_ready_at_a_cutoff_past_their_availability(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """So the ingestion is complete and correct; only the cutoff is wrong.

    Same rows, same query, a cutoff one hour past the availability the
    bars actually carry. Full coverage, ready. That isolates the defect
    to the offset rather than to anything this module writes.
    """
    version_id, _, _ = _ingest(committing_engine, universe, tickers_for, checkpoint_dir, app_config)
    knowable = session_close(TARGET_DATE) + DEFAULT_LAG_POLICY.daily_bar + timedelta(hours=1)

    with committing_engine.connect() as conn:
        readiness = check_readiness(
            conn,
            scan_date=TARGET_DATE,
            as_of=knowable,
            universe_version_id=version_id,
        )

    assert readiness.delivered == len(TICKERS)
    assert readiness.coverage == 1.0
    assert readiness.ready


def test_raising_the_scan_offset_past_the_lag_is_the_whole_fix(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The one-number change, executed rather than described.

    `scan_offset_hours` 5 -> 17 makes `as_of_for` land an hour past the
    bar's availability, and the scanner sees the session it just
    ingested. Nothing else moves: same bars, same query, same universe.

    This is the recommendation Module 26 hands to whoever is next allowed
    to change `core/live_scanner/config.py`.
    """
    version_id, _, _ = _ingest(committing_engine, universe, tickers_for, checkpoint_dir, app_config)
    hours = DEFAULT_LAG_POLICY.daily_bar.total_seconds() / 3600.0 + 1.0
    patched = ScannerConfig(
        settings=ScannerSettings.from_definition(
            {"settings": {**ScannerSettings().as_dict(), "scan_offset_hours": hours}}
        )
    )

    with committing_engine.connect() as conn:
        readiness = check_readiness(
            conn,
            scan_date=TARGET_DATE,
            as_of=as_of_for(TARGET_DATE, patched),
            universe_version_id=version_id,
            config=patched,
        )

    assert readiness.ready
    assert readiness.delivered == len(TICKERS)


def test_the_run_reports_itself_unhealthy_rather_than_claiming_success(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """A job that wrote everything and achieved nothing must not exit 0.

    The run asks Module 18's own question and reports the answer, so the
    cron's exit code is 1 and somebody is told — instead of a green job
    every evening beside a scanner recording `DATA_NOT_READY` every
    evening, with nothing connecting the two.
    """
    _, _, report = _ingest(committing_engine, universe, tickers_for, checkpoint_dir, app_config)

    assert report.prices.securities_written == len(TICKERS)
    assert not report.healthy
    assert report.readiness is not None
    assert not report.readiness.ready
