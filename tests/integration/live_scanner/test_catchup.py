"""Missed dates, after downtime: in order, exactly once, and bounded.

The bound is the part that matters most. "Incremental, never re-scans full
history" is an architectural property — the dual-mode interface makes any
date scannable — that has to be kept operationally. These tests are the
operational half.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from core.live_scanner.catchup import plan_catchup, run_catchup
from core.live_scanner.config import ScannerConfig, ScannerSetting, ScannerSettings
from core.live_scanner.daily import run_daily
from core.live_scanner.runs import completed_dates, run_history
from core.live_scanner.scanner import run_scan
from core.live_scanner.schedule import as_of_for
from core.scoring.engine import Lineage
from infra.db.enums import LiveScanStatus
from infra.db.schema.identity import universe_membership
from infra.db.schema.live_scanner import live_scan_runs
from tests.integration.feature_engine.conftest import insert_bars
from tests.integration.live_scanner.conftest import HISTORY_START, NOW, SCAN_DATE

#: The two-week window the catch-up tests walk, ending on the Tuesday the
#: rest of the suite uses. Short enough that a fixture can deliver bars
#: for every day in it.
WEEK_START = date(2021, 2, 22)


def _narrow(days: float = 12.0) -> ScannerConfig:
    """A catch-up window that reaches the fixture's history and no further."""
    return ScannerConfig(
        settings=ScannerSettings(
            max_catchup_days=ScannerSetting(value=days, kind="operational", rationale="test")
        )
    )


def _universe_through(
    connection: Connection, register, lineage: Lineage, *, count: int = 5
) -> list[UUID]:  # noqa: F821
    """Securities with bars running through the whole catch-up window."""
    length = len(pd.bdate_range(HISTORY_START.date(), SCAN_DATE))
    ids = []
    for index in range(count):
        security_id = register(f"CU{index}")
        insert_bars(
            connection,
            security_id,
            start=HISTORY_START,
            closes=[
                100.0 - 60.0 * min(n / (length * 0.55), 1.0) + index * 0.5 for n in range(length)
            ],
        )
        connection.execute(
            universe_membership.insert().values(
                universe_version_id=lineage.universe_version_id,
                security_id=security_id,
                listing_status="LISTED",
                listed_from=HISTORY_START,
                listed_to=None,
                exchange="NASDAQ",
                interval_evidence="reported",
            )
        )
        ids.append(security_id)
    return ids


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def test_a_fresh_system_plans_every_trading_day_in_the_window(
    connect, connection: Connection, lineage: Lineage
):
    plan = plan_catchup(connect, now=NOW, config=_narrow())

    assert len(plan) > 1
    assert plan.dates == sorted(plan.dates)
    assert SCAN_DATE in plan.dates
    assert all(day.weekday() < 5 for day in plan.dates)


def test_completed_dates_drop_out_of_the_plan(
    connect, connection: Connection, lineage: Lineage, modules, register
):
    _universe_through(connection, register, lineage)
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    plan = plan_catchup(connect, now=NOW, config=_narrow())

    assert SCAN_DATE not in plan.dates
    assert plan.already_complete == 1


def test_a_gap_longer_than_the_window_is_reported_rather_than_absorbed(
    connect, connection: Connection, lineage: Lineage
):
    """ "We are caught up" and "we are as caught up as the bound allows"
    are different claims, and only one of them is good news.

    Nothing has ever been scanned here, so the oldest day the window
    covers is itself outstanding — which means there are older dates the
    bound will never reach.
    """
    plan = plan_catchup(connect, now=NOW, config=_narrow(days=3.0))

    assert plan.truncated
    assert "will not be reached" in plan.summary()
    assert "operator decision" in plan.summary()


def test_a_scanner_that_is_keeping_up_does_not_report_a_gap(
    connect, connection: Connection, lineage: Lineage, modules, register
):
    """The signal is only worth having if it goes quiet when things are fine."""
    _universe_through(connection, register, lineage)
    config = _narrow(days=6.0)
    run_catchup(connect, now=NOW, lineage=lineage, modules=modules, config=config)

    # A day later, only the newest date is outstanding.
    later = NOW + timedelta(days=1)
    plan = plan_catchup(connect, now=later, config=config)

    assert not plan.truncated


def test_a_wide_window_with_everything_done_is_not_truncated(
    connect, connection: Connection, lineage: Lineage, modules, register
):
    _universe_through(connection, register, lineage)
    tiny = _narrow(days=1.0)
    run_catchup(connect, now=NOW, lineage=lineage, modules=modules, config=tiny)

    plan = plan_catchup(connect, now=NOW, config=tiny)

    assert plan.dates == []
    assert "Nothing outstanding" in plan.summary()


def test_the_catchup_window_bounds_how_far_back_the_scanner_looks(
    connect, connection: Connection, lineage: Lineage
):
    """The operational half of "never re-scans full history": a scheduling
    mistake cannot walk the scanner back through fifteen years."""
    wide = plan_catchup(connect, now=NOW, config=_narrow(days=25.0))
    narrow = plan_catchup(connect, now=NOW, config=_narrow(days=5.0))

    assert len(narrow) < len(wide)
    assert min(narrow.dates) > min(wide.dates)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def test_catchup_scans_missed_dates_oldest_first(
    connect, connection: Connection, lineage: Lineage, modules, register
):
    """A setup's lifecycle is a sequence. Scanning Thursday before
    Wednesday would advance state machines out of order."""
    _universe_through(connection, register, lineage)

    report = run_catchup(
        connect, now=NOW, lineage=lineage, modules=modules, config=_narrow(days=6.0)
    )

    scanned = [outcome.scan_date for outcome in report.outcomes]
    assert scanned == sorted(scanned)
    assert report.complete
    assert all(outcome.succeeded for outcome in report.outcomes)


def test_catchup_processes_each_date_exactly_once(
    connect, connection: Connection, lineage: Lineage, modules, register
):
    _universe_through(connection, register, lineage)
    config = _narrow(days=6.0)

    first = run_catchup(connect, now=NOW, lineage=lineage, modules=modules, config=config)
    second = run_catchup(connect, now=NOW, lineage=lineage, modules=modules, config=config)

    assert first.scanned > 1
    assert second.plan.dates == []
    assert second.outcomes == []

    rows = connection.execute(
        select(live_scan_runs.c.scan_date, func.count()).group_by(live_scan_runs.c.scan_date)
    ).all()
    assert all(count == 1 for _, count in rows)


def test_a_second_catchup_after_new_dates_appear_scans_only_the_new_ones(
    connect, connection: Connection, lineage: Lineage, modules, register
):
    _universe_through(connection, register, lineage)
    config = _narrow(days=6.0)
    # Past `PREVIOUS_DATE`'s own `due_at` (2021-03-02 14:00 UTC at the
    # default seventeen-hour offset) but before `SCAN_DATE`'s (the next
    # day, same clock time) — so the most recent date due is Monday, and
    # Tuesday is still `earlier`'s "new" date the second run picks up.
    earlier = datetime(2021, 3, 2, 18, 0, tzinfo=UTC)

    first = run_catchup(connect, now=earlier, lineage=lineage, modules=modules, config=config)
    already = {outcome.scan_date for outcome in first.outcomes}

    second = run_catchup(connect, now=NOW, lineage=lineage, modules=modules, config=config)

    assert SCAN_DATE not in already
    assert [outcome.scan_date for outcome in second.outcomes] == [SCAN_DATE]


def test_catchup_stops_at_the_first_date_that_does_not_finish(
    connect, connection: Connection, lineage: Lineage, modules, register, no_sleep
):
    """Scanning a later date on top of a missing earlier one would compute
    it against a state that never existed. A stopped catch-up is
    recoverable; one with a hole in it is not."""
    _universe_through(connection, register, lineage)
    config = _narrow(days=6.0)
    plan = plan_catchup(connect, now=NOW, config=config)
    doomed = plan.dates[1]

    real = __import__(
        "core.model_validation_evaluation.validation.replay", fromlist=["scan_one_date"]
    ).scan_one_date

    def explode_on_one(connection_, *, as_of, **kwargs):
        if as_of == as_of_for(doomed):
            raise ValueError("this day is cursed")
        return real(connection_, as_of=as_of, **kwargs)

    with patch("core.live_scanner.scanner.scan_one_date", side_effect=explode_on_one):
        report = run_catchup(
            connect,
            now=NOW,
            lineage=lineage,
            modules=modules,
            config=config,
            sleep=no_sleep,
        )

    assert not report.complete
    assert report.stopped_at == doomed
    assert "would compute it against a state that never existed" in report.stop_reason
    # The date before it did run; nothing after it did.
    assert [outcome.scan_date for outcome in report.outcomes] == [plan.dates[0], doomed]


def test_a_stopped_catchup_resumes_from_where_it_stopped(
    connect, connection: Connection, lineage: Lineage, modules, register, no_sleep
):
    """The recovery half. A stopped catch-up leaves the remaining dates
    outstanding, which is the whole reason stopping is safe."""
    _universe_through(connection, register, lineage)
    config = _narrow(days=6.0)
    plan = plan_catchup(connect, now=NOW, config=config)
    doomed = plan.dates[1]

    real = __import__(
        "core.model_validation_evaluation.validation.replay", fromlist=["scan_one_date"]
    ).scan_one_date

    def explode_once(connection_, *, as_of, **kwargs):
        if as_of == as_of_for(doomed):
            raise ValueError("transient-looking but not")
        return real(connection_, as_of=as_of, **kwargs)

    with patch("core.live_scanner.scanner.scan_one_date", side_effect=explode_once):
        run_catchup(
            connect, now=NOW, lineage=lineage, modules=modules, config=config, sleep=no_sleep
        )

    # The obstacle clears; the rest of the window runs.
    resumed = run_catchup(connect, now=NOW, lineage=lineage, modules=modules, config=config)

    assert resumed.complete
    assert resumed.plan.dates[0] == doomed
    assert completed_dates(connection) >= set(plan.dates)


def test_a_date_that_is_not_ready_stops_catchup_without_being_a_failure(
    connect, connection: Connection, lineage: Lineage, modules, register
):
    """Waiting is not the same as breaking, but it still stops the queue —
    the dates behind it cannot be scanned out of order either way."""
    _universe_through(connection, register, lineage, count=1)
    # One security, and no bars for the most recent dates, so coverage is
    # zero on the dates whose data has not arrived.
    config = _narrow(days=6.0)

    report = run_catchup(connect, now=NOW, lineage=lineage, modules=modules, config=config)

    if not report.complete:
        stopped = [
            outcome for outcome in report.outcomes if outcome.scan_date == report.stopped_at
        ][0]
        assert stopped.status in (
            LiveScanStatus.DATA_NOT_READY,
            LiveScanStatus.COMPLETED,
            LiveScanStatus.COMPLETED_WITH_EXCLUSIONS,
        )


# --------------------------------------------------------------------------
# The daily entry point
# --------------------------------------------------------------------------


def test_the_daily_entry_point_scans_whatever_is_outstanding(
    connect, connection: Connection, lineage: Lineage, modules, register
):
    """There is no 'normal path' plus a 'recovery path' somebody remembers
    to run after an outage — the outage is exactly when nobody remembers."""
    _universe_through(connection, register, lineage)

    report = run_daily(connect, lineage=lineage, now=NOW, modules=modules, config=_narrow(days=6.0))

    assert report.scanned > 1
    assert report.healthy
    assert SCAN_DATE in {outcome.scan_date for outcome in report.catchup.outcomes}


def test_calling_the_daily_entry_point_repeatedly_is_safe(
    connect, connection: Connection, lineage: Lineage, modules, register
):
    """The scheduler's only job is to call this often enough. Calling it
    every thirty minutes and calling it once must both produce a correct
    day."""
    _universe_through(connection, register, lineage)
    config = _narrow(days=6.0)

    first = run_daily(connect, lineage=lineage, now=NOW, modules=modules, config=config)
    second = run_daily(connect, lineage=lineage, now=NOW, modules=modules, config=config)
    third = run_daily(
        connect,
        lineage=lineage,
        now=NOW + timedelta(minutes=30),
        modules=modules,
        config=config,
    )

    assert first.scanned > 0
    assert second.scanned == 0
    assert third.scanned == 0
    for scan_date in {outcome.scan_date for outcome in first.catchup.outcomes}:
        assert len(run_history(connection, scan_date)) == 1


def test_the_daily_report_reads_as_a_status_line(
    connect, connection: Connection, lineage: Lineage, modules, register
):
    """Structured status a later Module 23 can consume — not alerting,
    which is explicitly not this module's job."""
    _universe_through(connection, register, lineage)

    report = run_daily(connect, lineage=lineage, now=NOW, modules=modules, config=_narrow(days=6.0))
    payload = report.as_dict()

    assert payload["now"] == NOW.isoformat()
    assert payload["healthy"] is True
    assert payload["catchup"]["plan"]["count"] >= payload["scanned"]
    assert "Live scanner wake-up" in report.summary()


def test_a_wake_up_with_nothing_to_do_is_healthy(
    connect, connection: Connection, lineage: Lineage, modules, register
):
    """A quiet wake-up is the normal case and must not read as a problem."""
    _universe_through(connection, register, lineage)
    config = _narrow(days=2.0)
    run_daily(connect, lineage=lineage, now=NOW, modules=modules, config=config)

    quiet = run_daily(connect, lineage=lineage, now=NOW, modules=modules, config=config)

    assert quiet.scanned == 0
    assert quiet.healthy
    assert "Nothing outstanding" in quiet.summary()
