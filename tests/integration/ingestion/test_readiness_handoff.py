"""Does the scanner actually act on what this module writes? — yes, now.

Module 26 exists because Module 18's readiness check counts bars in
`canonical_ohlcv` and nothing was writing them. It now writes them, and
for a while that was not enough: the obvious test, "and therefore the
scanner reports ready", **failed** — for a reason in neither module's
code, which is exactly the kind of thing that stays invisible until
something asserts it.

## The arithmetic that was wrong

Module 05 derives a daily bar's `availability_time` as its session close
plus `ProviderLagPolicy.daily_bar`, which is **sixteen hours**: FMP
publishes EOD data within hours, but consolidated values settle
overnight, so claiming same-session knowledge of a close would be
claiming more than is true.

Module 18's point-in-time cutoff for scanning that same session used to
be its close plus `scan_offset_hours`, which was **five**.
`readiness.check_readiness` filters on `availability_time <= as_of`, and
sixteen is not less than or equal to five — so a bar this module wrote
was never *knowable* at the cutoff the scanner applied to it, and
coverage read zero however complete the ingestion was. The scanner would
have recorded `DATA_NOT_READY` forever.

## Why the existing tests never caught it

Module 18's own integration fixtures insert bars with a one-hour
availability lag (`tests/integration/feature_engine/conftest.py`), which
is fifteen hours more optimistic than what Module 05 actually stamps on a
bar in production. Every readiness test passed against a bar no
production path could produce — until this file's tests exercised the
real lag against the real offset and did not.

## The fix, and why it is two numbers, not one

`docs/architecture/KNOWN_ISSUES.md` G1 (now **RESOLVED**) and
`core/live_scanner/config.py` carry the reasoning; the short version:

- `scan_offset_hours` moved from 5 to **17** — one hour past the bar
  lag, so a bar this module writes is knowable the first time the
  scanner asks.
- `readiness_window_hours` moved from 12 to **24**, because
  `next_scan_time` treats a date whose `due_at` (close + offset) has
  already passed its own `expires_at` (close + window) as broken on the
  very first check, with no retry ever attempted. Raising the offset
  alone, with the window left at 12, would have traded "permanently
  not-ready" for "permanently broken with zero retries" — a different
  failure with the same practical result. Both had to move together.

The tag on `scan_offset_hours` is still worth a second look:
`operational`, whose stated meaning is "bounds how the computation runs,
never what it produces". It feeds `as_of`, and `as_of` decides what a
scan can see. That is not operational, and stays flagged as such in
`core/live_scanner/config.py`'s own rationale text even though the value
itself is fixed.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from core.ingestion.config import IngestionConfig
from core.ingestion.orchestrator import run_daily_ingestion
from core.live_scanner.config import ScannerConfig
from core.live_scanner.readiness import check_readiness
from core.live_scanner.schedule import as_of_for, window_for
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
    """The half that always worked: a day with no prior data gets full coverage."""
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


def test_the_scan_offset_now_exceeds_the_bar_availability_lag():
    """The finding, as two numbers rather than a story.

    Nothing about this test needs a database or an ingestion run. It is
    here because it is the *cause* of the tests below, and reading it
    next to them is the point. Before the fix this asserted `lag >
    offset` — the broken relationship. It now asserts the opposite, so a
    future edit that reintroduces the mismatch fails here first, with the
    two literal values in the message rather than a downstream readiness
    puzzle.
    """
    lag = DEFAULT_LAG_POLICY.daily_bar
    offset = ScannerConfig().settings.scan_offset

    assert offset > lag
    assert lag == timedelta(hours=16)
    assert offset == timedelta(hours=17)


def test_the_readiness_window_still_exceeds_the_offset():
    """The second half of the fix, guarded on its own.

    `next_scan_time` treats a date whose `due_at` has already passed its
    own `expires_at` as broken on the very first check — no retry ever
    attempted. Raising `scan_offset_hours` without also raising
    `readiness_window_hours` would trade one permanent failure for
    another, silently. This is the structural invariant that keeps that
    from happening again, checked against the schedule module's own
    `window_for` rather than by comparing the two settings directly, so
    it fails the same way production would.
    """
    window = window_for(TARGET_DATE)

    assert window.expires_at > window.due_at


def test_the_scanner_now_sees_the_session_it_just_ingested(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """The honest answer to "does Module 18 stop being a permanent no-op" — yes.

    A complete ingestion, checked at the scanner's own default cutoff,
    with no patched configuration anywhere in this test. This is what
    G1's fix was for, and it is what a live deployment now does on its
    own, unaided.
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
    assert readiness.delivered == len(TICKERS)
    assert readiness.ready


def test_the_same_bars_were_already_ready_one_hour_past_their_availability(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """Isolates the claim from the scanner's own offset.

    Same rows, same query, a cutoff derived directly from the bar's own
    availability rather than from `scan_offset_hours` at all. Full
    coverage, ready — which is what proves the ingested data was correct
    all along and the earlier failure was purely in the cutoff applied to
    it, not in anything this module writes.
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


def test_the_run_now_reports_itself_healthy(
    committing_engine, universe, tickers_for, checkpoint_dir, app_config
):
    """A job that wrote everything and achieved something must say so.

    The run asks Module 18's own question and reports the answer. Before
    the fix this was asserted the other way — `not report.healthy` — as
    the honest, unhelpful state of a job that could never succeed. It now
    reports `healthy`, which is what makes the ingestion cron's exit code
    meaningful again: `0` means the session it fetched is scannable, not
    merely that no exception was raised.
    """
    _, _, report = _ingest(committing_engine, universe, tickers_for, checkpoint_dir, app_config)

    assert report.prices.securities_written == len(TICKERS)
    assert report.healthy
    assert report.readiness is not None
    assert report.readiness.ready
