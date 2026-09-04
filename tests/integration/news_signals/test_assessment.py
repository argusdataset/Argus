"""The aggregate query against a real `canonical_news`, and the PIT filter on it.

`evaluate()` itself is unit-tested against plain counts in
`tests/unit/news_signals/test_evaluate.py`. What can only be tested here is
whether the SQL that produces those counts gets the calendar-day buckets,
the trailing window, and the PIT `availability_time` cutoff right against a
real PostgreSQL — the same division of labour Module 12 draws between its
flag logic and its leakage tests.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from core.data_validation.result import MissReason
from core.news_signals.assessment import assess_news_volume
from core.news_signals.config import NewsSignalConfig
from core.news_signals.queries import news_counts_for

SCAN_DATE = date(2026, 3, 10)
AS_OF = datetime(2026, 3, 10, 21, 0, tzinfo=UTC)


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def test_a_security_with_no_articles_at_all_is_absent_from_the_query_result(connection, register):
    """`GROUP BY` has nothing to group. Documented in `queries.py`: callers
    must treat the missing key as `NewsCounts(0, 0, None)`."""
    security_id = register("ZZNONE")

    counts = news_counts_for(
        connection,
        [security_id],
        scan_date=SCAN_DATE,
        as_of=AS_OF,
        thresholds=NewsSignalConfig().thresholds,
    )

    assert security_id not in counts


def test_zero_news_ever_makes_the_single_security_assessment_undetermined(connection, register):
    security_id = register("ZZNEVER")

    signal = assess_news_volume(connection, security_id, scan_date=SCAN_DATE, as_of=AS_OF)

    assert signal.raised is None
    assert signal.unavailable is MissReason.NEVER_INGESTED
    assert signal.today_count == 0


def test_todays_articles_land_in_today_count_not_the_baseline(connection, register, add_article):
    security_id = register("ZZTODAY")
    thresholds = NewsSignalConfig().thresholds
    long_ago = SCAN_DATE - thresholds.window - timedelta(days=30)
    add_article(security_id, published=_at(long_ago))
    for _ in range(3):
        add_article(security_id, published=_at(SCAN_DATE, hour=9))

    counts = news_counts_for(
        connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF, thresholds=thresholds
    )[security_id]

    assert counts.today_count == 3
    assert counts.baseline_total == 0


def test_an_article_published_after_as_of_is_not_counted_at_all(connection, register, add_article):
    """PIT correctness: an article filed after the cutoff must not appear
    in today's bucket, even though its `event_time` falls on `scan_date`."""
    security_id = register("ZZLATE")
    thresholds = NewsSignalConfig().thresholds
    long_ago = SCAN_DATE - thresholds.window - timedelta(days=30)
    add_article(security_id, published=_at(long_ago))
    # Published (event_time) today, but not yet available as of AS_OF.
    add_article(security_id, published=_at(SCAN_DATE, hour=9), available=AS_OF + timedelta(hours=1))

    counts = news_counts_for(
        connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF, thresholds=thresholds
    )[security_id]

    assert counts.today_count == 0


def test_an_article_knowable_only_late_does_not_leak_into_an_earlier_as_of(
    connection, register, add_article
):
    """The mirror case, phrased as a replay: assessing an earlier `as_of`
    must not see an article that became available only afterward, even
    though the same query for the later `as_of` would."""
    security_id = register("ZZREPLAY")
    thresholds = NewsSignalConfig().thresholds
    long_ago = SCAN_DATE - thresholds.window - timedelta(days=30)
    add_article(security_id, published=_at(long_ago))
    add_article(security_id, published=_at(SCAN_DATE, hour=9), available=_at(SCAN_DATE, hour=20))

    earlier_as_of = _at(SCAN_DATE, hour=10)
    counts_early = news_counts_for(
        connection, [security_id], scan_date=SCAN_DATE, as_of=earlier_as_of, thresholds=thresholds
    )[security_id]
    counts_late = news_counts_for(
        connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF, thresholds=thresholds
    )[security_id]

    assert counts_early.today_count == 0
    assert counts_late.today_count == 1


def test_baseline_window_excludes_articles_older_than_the_window(connection, register, add_article):
    security_id = register("ZZOLD")
    thresholds = NewsSignalConfig().thresholds
    inside_window = SCAN_DATE - timedelta(days=5)
    outside_window = SCAN_DATE - thresholds.window - timedelta(days=1)
    add_article(security_id, published=_at(outside_window))
    add_article(security_id, published=_at(inside_window))

    counts = news_counts_for(
        connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF, thresholds=thresholds
    )[security_id]

    # earliest_event_date reflects the true oldest article (used by
    # `evaluate()` to decide determinability), but baseline_total counts
    # only what falls inside the trailing window.
    assert counts.earliest_event_date == outside_window
    assert counts.baseline_total == 1


def test_the_earliest_known_date_drives_the_undetermined_verdict_for_a_recent_name(
    connection, register, add_article
):
    security_id = register("ZZRECENT")
    first_seen = SCAN_DATE - timedelta(days=9)
    add_article(security_id, published=_at(first_seen))

    signal = assess_news_volume(connection, security_id, scan_date=SCAN_DATE, as_of=AS_OF)

    assert signal.raised is None
    assert signal.unavailable is MissReason.NOT_YET_AVAILABLE


def test_a_fully_observed_security_produces_a_determined_verdict(connection, register, add_article):
    security_id = register("ZZFULL")
    thresholds = NewsSignalConfig().thresholds
    long_ago = SCAN_DATE - thresholds.window - timedelta(days=100)
    add_article(security_id, published=_at(long_ago))
    # A quiet, roughly two-per-day baseline.
    for offset in range(1, thresholds.window_days + 1):
        add_article(security_id, published=_at(SCAN_DATE - timedelta(days=offset)))
        add_article(security_id, published=_at(SCAN_DATE - timedelta(days=offset), hour=15))
    for _ in range(10):
        add_article(security_id, published=_at(SCAN_DATE, hour=9))

    signal = assess_news_volume(connection, security_id, scan_date=SCAN_DATE, as_of=AS_OF)

    assert signal.determined is True
    assert signal.raised is True
    assert signal.today_count == 10
    assert signal.baseline_mean == pytest.approx(2.0)
