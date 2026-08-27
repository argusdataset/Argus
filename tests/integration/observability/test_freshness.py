"""The four-state freshness model, one test per state and one per confusion."""

from __future__ import annotations

from datetime import timedelta

import pytest

from infra.observability.config import ObservabilitySettings
from infra.observability.freshness import (
    FEEDS,
    FreshnessState,
    all_feeds,
    feed_freshness,
)
from tests.integration.observability.conftest import NOW

SETTINGS = ObservabilitySettings()


def test_a_recent_arrival_is_fresh(connection, register, add_bar):
    security_id = register("FRESH")
    add_bar(security_id, event_time=NOW - timedelta(hours=3))

    result = feed_freshness(connection, "ohlcv", now=NOW)

    assert result.state is FreshnessState.FRESH
    assert result.healthy is True
    assert result.age_seconds is not None
    assert result.age_seconds < SETTINGS.fresh_within_seconds


def test_a_weekend_gap_is_delayed_and_not_a_problem(connection, register, add_bar):
    """Friday's close read on Monday morning. Nothing is wrong."""
    security_id = register("DELAYED")
    add_bar(
        security_id, event_time=NOW - timedelta(hours=64), available_at=NOW - timedelta(hours=62)
    )

    result = feed_freshness(connection, "ohlcv", now=NOW)

    assert result.state is FreshnessState.DELAYED
    assert result.healthy is True, "delayed is explicable, not an incident"


def test_a_long_silence_is_stale_and_says_how_long(connection, register, add_bar):
    security_id = register("STALE")
    add_bar(security_id, event_time=NOW - timedelta(days=9), available_at=NOW - timedelta(days=9))

    result = feed_freshness(connection, "ohlcv", now=NOW)

    assert result.state is FreshnessState.STALE
    assert result.healthy is False
    assert "216 hour(s) ago" in result.explanation


def test_a_feed_that_never_delivered_is_unavailable_not_stale(connection):
    """The distinction this file exists for.

    A never-ingested feed is a configuration problem; a stopped feed is an
    outage. A dashboard showing both as "stale, ∞ hours" makes them
    indistinguishable, and they want different people.
    """
    result = feed_freshness(connection, "ohlcv", now=NOW)

    assert result.state is FreshnessState.UNAVAILABLE
    assert result.row_count == 0
    assert "never" in result.explanation.lower()


def test_unavailable_reports_no_age_rather_than_an_age_of_zero(connection):
    """`None`, never `0.0` — the rule every module since 08 has held.

    Zero would read as "arrived just now", which is the opposite of what
    is true.
    """
    result = feed_freshness(connection, "ohlcv", now=NOW)

    assert result.age_seconds is None
    assert result.latest_availability is None


def test_freshness_is_measured_on_availability_not_event_time(connection, register, add_bar):
    """A correct backfill is a feed working, not an outage.

    A bar for a session two years ago, ingested five minutes ago, is
    ARGUS learning something promptly. Measuring `event_time` would report
    the backfill as a two-year-old feed.
    """
    security_id = register("BACKFILL")
    add_bar(
        security_id,
        event_time=NOW - timedelta(days=730),
        available_at=NOW - timedelta(minutes=5),
    )

    result = feed_freshness(connection, "ohlcv", now=NOW)

    assert result.state is FreshnessState.FRESH


def test_the_newest_row_decides_regardless_of_how_old_the_others_are(connection, register, add_bar):
    security_id = register("MIXED")
    for days in (400, 200, 30):
        add_bar(
            security_id,
            event_time=NOW - timedelta(days=days),
            available_at=NOW - timedelta(days=days),
        )
    add_bar(security_id, event_time=NOW - timedelta(hours=2), available_at=NOW - timedelta(hours=2))

    result = feed_freshness(connection, "ohlcv", now=NOW)

    assert result.state is FreshnessState.FRESH
    assert result.row_count == 4


def test_freshness_can_be_scoped_to_one_security(connection, register, add_bar):
    """A universe-wide feed can be fresh while one security has gone quiet."""
    lively = register("LIVELY")
    quiet = register("QUIET")
    add_bar(lively, event_time=NOW - timedelta(hours=2), available_at=NOW - timedelta(hours=2))
    add_bar(quiet, event_time=NOW - timedelta(days=20), available_at=NOW - timedelta(days=20))

    assert feed_freshness(connection, "ohlcv", now=NOW).state is FreshnessState.FRESH
    scoped = feed_freshness(connection, "ohlcv", security_id=quiet, now=NOW)
    assert scoped.state is FreshnessState.STALE
    assert scoped.security_id == quiet


def test_an_unknown_feed_names_the_ones_that_exist(connection):
    with pytest.raises(KeyError, match="not a watched feed"):
        feed_freshness(connection, "sentiment", now=NOW)


def test_every_watched_feed_is_reported_in_a_stable_order(connection):
    """So two reads compare cleanly rather than by luck of iteration."""
    results = all_feeds(connection, now=NOW)

    assert [item.feed for item in results] == sorted(FEEDS)
    assert all(item.state is FreshnessState.UNAVAILABLE for item in results)


# --------------------------------------------------------------------------
# The bridge back to services/shared/
# --------------------------------------------------------------------------


def test_the_shared_freshness_shape_is_produced_not_reinvented(connection, register, add_bar):
    """Modules 19-21 serve `Freshness`. This produces exactly that object.

    One computation, two audiences — rather than a monitor whose numbers
    drift from the ones the API is publishing.
    """
    from services.shared.schemas import Freshness

    security_id = register("SHARED")
    add_bar(security_id, event_time=NOW - timedelta(hours=2), available_at=NOW - timedelta(hours=2))

    result = feed_freshness(connection, "ohlcv", now=NOW)
    shared = result.as_freshness()

    assert isinstance(shared, Freshness)
    assert shared.stale is False
    assert shared.as_of == NOW
    assert shared.computed_at == result.latest_availability
    assert shared.age_seconds == pytest.approx(result.age_seconds)


def test_stale_and_unavailable_both_read_as_stale_to_an_api_consumer(connection, register, add_bar):
    """The shared shape carries a boolean; the four-state view carries the difference.

    From a response's side both mean "do not treat this as current", which
    is all that shape can say. The operator distinction lives on the
    monitor object, which is why the monitor is not just a `Freshness`.
    """
    unavailable = feed_freshness(connection, "news", now=NOW).as_freshness()

    security_id = register("OLD")
    add_bar(security_id, event_time=NOW - timedelta(days=30), available_at=NOW - timedelta(days=30))
    stale = feed_freshness(connection, "ohlcv", now=NOW).as_freshness()

    assert unavailable.stale is True
    assert stale.stale is True
    assert unavailable.staleness_reason != stale.staleness_reason, (
        "the reason still tells them apart"
    )
