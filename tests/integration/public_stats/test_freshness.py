"""Materialization: cheap reads, honest ages, and no withdrawn figures.

The point of storing payloads is that a read is one lookup. The point of
storing the *gate state* alongside them is that a stored payload can be
refused when it stops being true. These test both halves, and the second
one harder.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from infra.db.schema.public_stats import public_stat_snapshots
from services.public_stats.aggregates import CHARTS
from services.public_stats.config import (
    PublicStatsConfig,
    PublicStatsSetting,
    PublicStatsSettings,
)
from services.public_stats.gate import current_scope
from services.public_stats.snapshots import read_chart, refresh_public_stats
from tests.integration.public_stats.conftest import AS_OF


@pytest.fixture
def approved(connection, make_run, reviewer, seed_outcomes):
    make_run(status="APPROVED", reviewer_id=reviewer)
    seed_outcomes(40, prefix="FR")
    return connection


def test_a_refresh_stores_one_row_per_chart(approved):
    sizes = refresh_public_stats(approved, as_of=AS_OF)

    stored = approved.execute(select(func.count()).select_from(public_stat_snapshots)).scalar_one()

    assert set(sizes) == set(CHARTS)
    assert stored == len(CHARTS)


def test_a_second_refresh_replaces_rather_than_accumulates(approved):
    """A snapshot is a cache. Keeping every version would turn this table
    into a slow-growing log nobody reads — the audited record of what was
    publishable lives in the gate tables, which are append-only."""
    refresh_public_stats(approved, as_of=AS_OF)
    refresh_public_stats(approved, as_of=AS_OF)

    stored = approved.execute(select(func.count()).select_from(public_stat_snapshots)).scalar_one()

    assert stored == len(CHARTS)


def test_a_stored_chart_records_the_gate_state_it_was_computed_under(approved):
    """The column that makes materialization safe rather than dangerous."""
    refresh_public_stats(approved, as_of=AS_OF)
    scope = current_scope(approved)

    row = approved.execute(select(public_stat_snapshots).limit(1)).one()

    assert row.gate_fingerprint == scope.fingerprint()
    assert len(row.included_runs) == 1
    assert row.as_of == AS_OF


def test_reading_a_chart_reports_its_age(approved):
    refresh_public_stats(approved, as_of=AS_OF, now=AS_OF)

    stored = read_chart(approved, "win_rate", now=AS_OF + timedelta(hours=3))

    assert stored.age_seconds(AS_OF + timedelta(hours=3)) == pytest.approx(10_800)
    assert stored.stale is False


def test_an_old_snapshot_is_marked_stale_but_still_served(approved):
    """A day-old true number beats a spinner. The age is reported so the
    reader can judge it rather than having ARGUS judge for them."""
    refresh_public_stats(approved, as_of=AS_OF, now=AS_OF)

    stored = read_chart(approved, "win_rate", now=AS_OF + timedelta(hours=30))

    assert stored.stale is True
    assert "refresh cadence" in stored.staleness_reason
    assert stored.payload["sample_size"] == 40


def test_the_refresh_cadence_is_configuration(approved):
    impatient = PublicStatsConfig(
        settings=PublicStatsSettings(
            expected_refresh_hours=PublicStatsSetting(
                value=1.0, kind="operational", rationale="test"
            )
        )
    )
    refresh_public_stats(approved, as_of=AS_OF, now=AS_OF)

    fresh = read_chart(approved, "win_rate", now=AS_OF + timedelta(minutes=30))
    stale = read_chart(approved, "win_rate", config=impatient, now=AS_OF + timedelta(minutes=90))

    assert fresh.stale is False
    assert stale.stale is True


def test_reading_before_any_refresh_is_a_503_not_an_empty_chart(connection, make_run, reviewer):
    """Nothing published yet is a different fact from statistics of zero."""
    from services.public_stats.errors import PublicStatsError

    make_run(status="APPROVED", reviewer_id=reviewer)

    with pytest.raises(PublicStatsError) as exc_info:
        read_chart(connection, "win_rate")

    assert exc_info.value.code == "STATISTICS_UNAVAILABLE"
    assert exc_info.value.status == 503


def test_every_chart_is_computed_from_one_dataset_per_refresh(approved):
    """Two charts on the same page cannot disagree about the population
    behind them, because there is one load per refresh rather than one
    per chart."""
    refresh_public_stats(approved, as_of=AS_OF)

    rows = approved.execute(
        select(public_stat_snapshots.c.as_of, public_stat_snapshots.c.gate_fingerprint)
    ).all()

    assert len({row.as_of for row in rows}) == 1
    assert len({row.gate_fingerprint for row in rows}) == 1


def test_the_fingerprint_is_stable_across_query_order(approved):
    """Sorted, so the same approved set always hashes the same. An
    order-dependent fingerprint would flag every read as withdrawn."""
    first = current_scope(approved).fingerprint()
    second = current_scope(approved).fingerprint()

    assert first == second


def test_the_fingerprint_changes_when_a_window_is_approved(approved, lineage, reviewer):
    from services.public_stats.releases import approve_window, open_window_for_review
    from tests.integration.public_stats.conftest import LIVE_END, LIVE_START

    before = current_scope(approved).fingerprint()
    open_window_for_review(
        approved,
        period_start=LIVE_START,
        period_end=LIVE_END,
        data_snapshot_id=lineage.data_snapshot_id,
    )
    approve_window(approved, period_start=LIVE_START, period_end=LIVE_END, user_id=reviewer)

    assert current_scope(approved).fingerprint() != before


def test_the_public_release_history_is_itself_public(client, approved, lineage, reviewer):
    """A gate whose decisions were private would be a claim nobody could
    check. Including the rejections — a gate that published only its
    approvals would be a record of what ARGUS wanted shown."""
    from services.public_stats.releases import (
        approve_window,
        open_window_for_review,
        reject_window,
    )
    from tests.integration.public_stats.conftest import LIVE_END, LIVE_START

    open_window_for_review(
        approved,
        period_start=LIVE_START,
        period_end=LIVE_END,
        data_snapshot_id=lineage.data_snapshot_id,
    )
    approve_window(approved, period_start=LIVE_START, period_end=LIVE_END, user_id=reviewer)
    reject_window(
        approved,
        period_start=LIVE_START,
        period_end=LIVE_END,
        user_id=reviewer,
        note="withdrawn",
    )

    history = client.get(f"/public/releases/{LIVE_START}/{LIVE_END}").json()

    assert [row["status"] for row in history] == ["PENDING_REVIEW", "APPROVED", "REJECTED"]
    assert history[0]["by_system"] is True
    assert history[1]["by_system"] is False
    # The reviewer's identity is not published — that a named human
    # decided is the checkable fact; who they were is not.
    assert all("assigned_by_user_id" not in row for row in history)


def test_only_currently_approved_windows_are_listed(client, approved, lineage, reviewer):
    from services.public_stats.releases import (
        approve_window,
        open_window_for_review,
        reject_window,
    )
    from tests.integration.public_stats.conftest import LIVE_END, LIVE_START

    open_window_for_review(
        approved,
        period_start=LIVE_START,
        period_end=LIVE_END,
        data_snapshot_id=lineage.data_snapshot_id,
    )
    approve_window(approved, period_start=LIVE_START, period_end=LIVE_END, user_id=reviewer)
    assert len(client.get("/public/releases").json()) == 1

    reject_window(approved, period_start=LIVE_START, period_end=LIVE_END, user_id=reviewer)

    assert client.get("/public/releases").json() == []


def test_the_public_api_has_no_authentication_anywhere(client, approved):
    """This module is public by design. No endpoint may read an identity,
    and none may answer differently for different callers."""
    refresh_public_stats(approved, as_of=AS_OF)

    plain = client.get("/public/stats/win_rate")
    with_header = client.get(
        "/public/stats/win_rate", headers={"X-Argus-User": "00000000-0000-0000-0000-000000000000"}
    )

    assert plain.status_code == with_header.status_code == 200
    assert plain.json()["series"] == with_header.json()["series"]
