"""Batch assessment, the upsert, and the daily orchestrator run.

`assess_batch` is one aggregate query over however many securities are
passed; `store_signals` is the only place this module writes. Tested
together with the orchestrator because what the orchestrator adds is the
scheduling decision (`scan_date_for`) and the universe membership lookup,
not a different write path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from core.news_signals.batch import assess_batch, store_signals
from core.news_signals.config import NewsSignalConfig
from core.news_signals.orchestrator import run_daily_news_signals
from infra.db.schema.news_signals import news_volume_signals
from infra.db.schema.sec_filings import sec_filing_signals

SCAN_DATE = date(2026, 3, 10)
AS_OF = datetime(2026, 3, 10, 21, 0, tzinfo=UTC)

#: A Tuesday evening, after Monday's session has closed and Module 18's own
#: schedule considers it due — the same NOW Module 26's integration suite
#: uses for the identical reason. With `scan_offset_hours` at 17 (see
#: KNOWN_ISSUES.md G1), the session that is due by this instant is
#: Monday, not Tuesday itself.
NOW = datetime(2026, 3, 10, 21, 0, tzinfo=UTC)
ORCHESTRATOR_SCAN_DATE = date(2026, 3, 9)
#: A Saturday: no trading session is due at all.
WEEKEND_NOW = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


# --------------------------------------------------------------------------
# assess_batch: one query, many verdicts
# --------------------------------------------------------------------------


def test_an_empty_security_list_produces_an_empty_result_with_no_query(connection):
    assert assess_batch(connection, [], scan_date=SCAN_DATE, as_of=AS_OF) == {}


def test_assess_batch_gives_each_security_its_own_independent_verdict(
    connection, register, add_article
):
    never = register("ZZBNEVER")

    thresholds = NewsSignalConfig().thresholds
    quiet = register("ZZBQUIET")
    # A full window of one-article-a-day history (baseline_mean == 1.0),
    # plus today's single article — below the 3x multiple, so this reads
    # as ordinary volume rather than an anomaly.
    for offset in range(1, thresholds.window_days + 1):
        add_article(quiet, published=_at(SCAN_DATE - timedelta(days=offset)))
    add_article(quiet, published=_at(SCAN_DATE))

    signals = assess_batch(connection, [never, quiet], scan_date=SCAN_DATE, as_of=AS_OF)

    assert signals[never].raised is None
    assert signals[quiet].raised is False
    assert signals[quiet].baseline_mean == pytest.approx(1.0)


# --------------------------------------------------------------------------
# store_signals: upsert, one row per (security_id, signal_date)
# --------------------------------------------------------------------------


def test_store_signals_writes_a_retrievable_row(connection, register, news_signal_row):
    security_id = register("ZZSTORE")
    signals = assess_batch(connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF)

    written = store_signals(connection, list(signals.values()))

    assert written == 1
    row = news_signal_row(security_id)
    assert row is not None
    assert row.signal_date == SCAN_DATE
    assert row.raised is None
    assert row.unavailable_reason == "never_ingested"


def test_a_same_day_rerun_overwrites_rather_than_duplicates(
    connection, register, add_article, news_signal_row
):
    """`(security_id, signal_date)` is unique. A rerun after new articles
    arrive must converge on one corrected row, not accumulate a second."""
    security_id = register("ZZRERUN")

    first_pass = assess_batch(connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF)
    store_signals(connection, list(first_pass.values()))
    assert news_signal_row(security_id).raised is None

    thresholds = NewsSignalConfig().thresholds
    long_ago = SCAN_DATE - thresholds.window - timedelta(days=10)
    add_article(security_id, published=_at(long_ago))

    second_pass = assess_batch(connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF)
    store_signals(connection, list(second_pass.values()))

    rows = connection.execute(
        select(news_volume_signals).where(news_volume_signals.c.security_id == security_id)
    ).all()
    assert len(rows) == 1
    assert rows[0].raised is False
    assert rows[0].unavailable_reason is None


def test_a_different_days_signal_is_a_separate_row(connection, register, news_signal_row):
    security_id = register("ZZTWODAYS")
    other_date = SCAN_DATE - timedelta(days=1)

    for day, moment in ((SCAN_DATE, AS_OF), (other_date, AS_OF - timedelta(days=1))):
        signals = assess_batch(connection, [security_id], scan_date=day, as_of=moment)
        store_signals(connection, list(signals.values()))

    rows = connection.execute(
        select(news_volume_signals).where(news_volume_signals.c.security_id == security_id)
    ).all()
    assert {row.signal_date for row in rows} == {SCAN_DATE, other_date}


# --------------------------------------------------------------------------
# The daily orchestrator: schedule, universe membership, one transaction
# --------------------------------------------------------------------------


def test_a_run_with_nothing_due_skips_without_touching_the_database(monkeypatch, universe):
    """`scan_date_for` returning `None` is a rare calendar edge (the
    default 30-day catch-up window makes a genuine "nothing due" instant
    hard to construct honestly), so this substitutes it directly — the
    same technique `tests/unit/intelligence/test_boundaries.py` uses to
    prove a call is real rather than reading the source for a guard
    clause. Because the skip happens before `engine.begin()` is ever
    called, an engine that would raise if touched proves the database was
    never reached at all.
    """
    import core.news_signals.orchestrator as orchestrator_module

    version_id, _identities = universe(())
    monkeypatch.setattr(orchestrator_module, "scan_date_for", lambda moment, config=None: None)

    class _ExplodingEngine:
        def begin(self):
            raise AssertionError("the orchestrator touched the database with nothing due")

    report = run_daily_news_signals(
        _ExplodingEngine(), universe_version_id=version_id, now=WEEKEND_NOW
    )

    assert report.scan_date is None
    assert report.skipped_reason
    assert report.healthy is True
    assert report.stored == 0


def test_a_run_assesses_and_stores_every_universe_member(engine, universe, add_article_committed):
    """`ZZOQUIET` has one article well outside the baseline window — enough
    to make it determinable with a zero baseline and no coverage today, so
    it reads `raised=False`. `ZZOUNSEEN` has never had an article, so it
    stays undetermined. Both get a stored row regardless."""
    version_id, identities = universe(("ZZOQUIET", "ZZOUNSEEN"))
    quiet = identities["ZZOQUIET"]
    thresholds = NewsSignalConfig().thresholds
    add_article_committed(
        quiet, published=_at(ORCHESTRATOR_SCAN_DATE - thresholds.window - timedelta(days=10))
    )

    report = run_daily_news_signals(engine, universe_version_id=version_id, now=NOW)

    assert report.scan_date == ORCHESTRATOR_SCAN_DATE
    assert report.universe_size == 2
    assert report.stored == 2
    assert report.not_raised == 1
    assert report.undetermined == 1
    assert report.healthy is True

    with engine.connect() as verify:
        row = verify.execute(
            select(news_volume_signals).where(news_volume_signals.c.security_id == quiet)
        ).one_or_none()
    assert row is not None
    assert row.signal_date == ORCHESTRATOR_SCAN_DATE


def test_a_run_with_an_empty_universe_is_healthy(engine, universe):
    version_id, _identities = universe(())

    report = run_daily_news_signals(engine, universe_version_id=version_id, now=NOW)

    assert report.scan_date == ORCHESTRATOR_SCAN_DATE
    assert report.universe_size == 0
    assert report.stored == 0
    assert report.healthy is True


def test_the_run_report_serializes_to_a_complete_dict(engine, universe):
    version_id, _identities = universe(())

    report = run_daily_news_signals(engine, universe_version_id=version_id, now=NOW)
    payload = report.as_dict()

    assert payload["scan_date"] == ORCHESTRATOR_SCAN_DATE.isoformat()
    assert payload["healthy"] is True
    assert set(payload) == {
        "scan_date",
        "universe_size",
        "raised",
        "not_raised",
        "undetermined",
        "stored",
        # The 8-K signal shares this run and reports its own counters:
        # a run where the volume signal worked and the filing signal did
        # not should be visibly that, not an average of the two.
        "filings_raised",
        "filings_stored",
        "skipped_reason",
        "healthy",
    }


def test_one_run_computes_both_the_volume_and_the_filing_signal(
    engine, universe, add_article_committed
):
    """The two signals share a run, a universe and a scan date.

    Computing them together is what makes them agree about which day it
    is; the counters stay separate so the run can still say which of the
    two did the work.
    """
    version_id, identities = universe(("ZZBOTH",))

    report = run_daily_news_signals(engine, universe_version_id=version_id, now=NOW)

    assert report.scan_date == ORCHESTRATOR_SCAN_DATE
    assert report.universe_size == 1
    # A security with no news and no filings: both signals are computed
    # and stored, the volume one undetermined and the filing one a
    # measured False.
    assert report.stored == 1
    assert report.filings_stored == 1
    assert report.filings_raised == 0

    with engine.connect() as verify:
        row = verify.execute(
            select(sec_filing_signals).where(
                sec_filing_signals.c.security_id == identities["ZZBOTH"]
            )
        ).one_or_none()
    assert row is not None
    assert row.raised is False
    assert row.signal_date == ORCHESTRATOR_SCAN_DATE
