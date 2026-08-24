"""One day's scan, unattended: does it run, and is the result durably there?

The assertion this file cares most about is not "the scan returned
something" — it is "you can close the process, come back, query the
database, and find out what happened." Module 17's batch replay hands its
results to an evaluation that is about to read them. A live scan hands
them to nobody; storage is the only handoff there is.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from core.live_scanner.config import ScannerConfig, ScannerSetting, ScannerSettings
from core.live_scanner.readiness import check_readiness
from core.live_scanner.results import scan_results, setups_opened_by_scan, signals_for_scan
from core.live_scanner.runs import latest_run, run_history, runs_by_status
from core.live_scanner.scanner import run_scan
from core.live_scanner.schedule import as_of_for
from core.scoring.engine import Lineage
from infra.db.enums import LiveScanStatus
from infra.db.schema.live_scanner import live_scan_runs
from infra.db.schema.versioning import feature_vectors
from tests.integration.live_scanner.conftest import PREVIOUS_DATE, SCAN_DATE


def test_a_scan_runs_and_records_a_completed_run(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    populated_universe()
    outcome = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    assert outcome.status is LiveScanStatus.COMPLETED
    assert outcome.succeeded
    assert not outcome.skipped
    assert outcome.result is not None
    assert outcome.result.universe_size == 5


def test_the_run_record_survives_the_call_and_is_queryable(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """The handoff. A caller that threw away the return value must still
    be able to find out what happened."""
    populated_universe()
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    stored = latest_run(connection, SCAN_DATE)

    assert stored is not None
    assert stored.status is LiveScanStatus.COMPLETED
    assert stored.attempt == 0
    assert stored.as_of == as_of_for(SCAN_DATE)
    assert stored.finished_at is not None
    assert stored.detail["result"]["universe_size"] == 5


def test_the_run_records_the_full_lineage_it_scanned_under(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """A day's signals are attributable through their own rows; this makes
    the *run* attributable, which is what lets an investigation ask what
    changed between the day this worked and the day it did not."""
    populated_universe()
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    row = connection.execute(
        select(live_scan_runs).where(live_scan_runs.c.scan_date == SCAN_DATE)
    ).one()

    assert row.target_model_version_id == lineage.target_model_version_id
    assert row.feature_schema_version_id == lineage.feature_schema_version_id
    assert row.scoring_configuration_id == lineage.scoring_configuration_id
    assert row.universe_version_id == lineage.universe_version_id
    assert row.data_snapshot_id == lineage.data_snapshot_id


def test_the_scans_own_output_is_durably_stored_not_merely_returned(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """Module 17 returns its signals because its evaluation reads them
    immediately. Nothing downstream of a live scan does, so the test is
    that the rows are there rather than that the object is."""
    populated_universe()
    outcome = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    vectors = connection.execute(
        select(func.count())
        .select_from(feature_vectors)
        .where(feature_vectors.c.feature_schema_version_id == lineage.feature_schema_version_id)
    ).scalar_one()

    assert vectors > 0
    # And the outcome object itself carries no signals to hand around.
    assert not hasattr(outcome, "signals")


def test_a_consumer_reads_the_days_results_by_date_without_knowing_the_cutoff(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """The indirection the run record exists for.

    Rows are stamped with `as_of` — the session close plus an offset — and
    a consumer asking "what did the 2021-03-02 scan produce" must not be
    made to reconstruct that.
    """
    populated_universe()
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    results = scan_results(connection, SCAN_DATE)

    assert results.available
    assert results.run is not None
    assert results.as_dict()["scan_date"] == SCAN_DATE.isoformat()
    # Signals and setups are reachable by date alone.
    assert isinstance(signals_for_scan(connection, SCAN_DATE), list)
    assert isinstance(setups_opened_by_scan(connection, SCAN_DATE), list)


def test_a_date_that_was_never_scanned_reads_as_unavailable_not_as_empty(connection: Connection):
    """ "Nobody has looked" and "we looked and found nothing" are different
    answers, and a consumer has to be able to tell them apart."""
    results = scan_results(connection, date(2021, 3, 4))

    assert not results.available
    assert results.run is None
    assert signals_for_scan(connection, date(2021, 3, 4)) == []


def test_a_quiet_day_is_a_successful_scan_not_an_unavailable_one(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """Today, every scan is quiet — Module 13 scores nothing until the
    case dataset exists. That must read as success, or the scanner would
    look broken for as long as it is working correctly."""
    populated_universe()
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    results = scan_results(connection, SCAN_DATE)

    assert results.available
    assert results.scored_signals == 0


def test_a_non_trading_day_is_skipped_and_writes_no_row(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """A weekend is not a scan that was skipped — it is a date that was
    never a scan date, and a row per weekend forever would make the table
    mostly noise."""
    populated_universe()
    saturday = date(2021, 3, 6)

    outcome = run_scan(connect, scan_date=saturday, lineage=lineage, modules=modules)

    assert outcome.skipped
    assert "not a US equity trading day" in outcome.reason
    assert latest_run(connection, saturday) is None
    assert connection.execute(select(func.count()).select_from(live_scan_runs)).scalar_one() == 0


def test_a_market_holiday_is_skipped_too(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """Module 07 already knows the calendar; the scanner asks it rather
    than modelling weekends and hoping holidays never matter."""
    populated_universe()
    good_friday = date(2021, 4, 2)

    outcome = run_scan(connect, scan_date=good_friday, lineage=lineage, modules=modules)

    assert outcome.skipped
    assert latest_run(connection, good_friday) is None


def test_a_day_whose_data_has_not_arrived_is_not_ready_rather_than_failed(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """The distinction the whole failure policy rests on.

    Two of five securities delivered a bar for the scan date. That is a
    delivery delay: the right response is to wait, and recording it as
    FAILED would train whoever reads these rows to ignore FAILED.
    """
    populated_universe(count=5, deliver=2)

    outcome = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    assert outcome.status is LiveScanStatus.DATA_NOT_READY
    assert not outcome.succeeded
    assert "40.0%" in outcome.reason
    assert "cross-sectional" in outcome.reason

    stored = latest_run(connection, SCAN_DATE)
    assert stored.status is LiveScanStatus.DATA_NOT_READY
    assert stored.detail["readiness"]["delivered"] == 2
    assert stored.detail["readiness"]["universe_size"] == 5


def test_a_not_ready_day_stays_outstanding_and_is_scanned_once_data_lands(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """Waiting has to actually resolve, or DATA_NOT_READY is just a nicer
    word for giving up."""
    securities = populated_universe(count=5, deliver=2)
    first = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)
    assert first.status is LiveScanStatus.DATA_NOT_READY

    # The rest of the feed arrives.
    import pandas as pd

    from tests.integration.feature_engine.conftest import insert_bars
    from tests.integration.live_scanner.conftest import HISTORY_START

    length = len(pd.bdate_range(HISTORY_START.date(), SCAN_DATE))
    for index, security_id in enumerate(securities[2:], start=2):
        insert_bars(
            connection,
            security_id,
            start=PREVIOUS_DATE + timedelta(days=1),
            closes=[41.0 + index],
        )
    assert length > 0

    second = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    assert second.status is LiveScanStatus.COMPLETED
    assert [run.status for run in run_history(connection, SCAN_DATE)] == [
        LiveScanStatus.DATA_NOT_READY,
        LiveScanStatus.COMPLETED,
    ]


def test_an_empty_universe_is_refused_rather_than_scanned_successfully(
    connect, connection: Connection, lineage: Lineage, modules
):
    """A clean, meaningless, successful-looking run of nothing is the most
    misleading result this module could record."""
    outcome = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    assert outcome.status is LiveScanStatus.DATA_NOT_READY
    assert "no members listed" in outcome.reason


def test_the_coverage_floor_is_configuration_and_moving_it_changes_the_verdict(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """`min_universe_coverage` is the one number here that decides whether
    a day gets scanned, which is why it is tagged calibratable."""
    populated_universe(count=5, deliver=3)
    as_of = as_of_for(SCAN_DATE)

    strict = check_readiness(
        connection,
        scan_date=SCAN_DATE,
        as_of=as_of,
        universe_version_id=lineage.universe_version_id,
    )
    lenient = check_readiness(
        connection,
        scan_date=SCAN_DATE,
        as_of=as_of,
        universe_version_id=lineage.universe_version_id,
        config=ScannerConfig(
            settings=ScannerSettings(
                min_universe_coverage=ScannerSetting(
                    value=0.5, kind="calibratable", rationale="test"
                )
            )
        ),
    )

    assert not strict.ready
    assert lenient.ready
    assert strict.coverage == pytest.approx(0.6)


def test_readiness_filters_on_availability_not_merely_presence(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """Otherwise the scanner could see data the scan would then refuse to
    look at, conclude it was ready, and scan an empty day."""
    populated_universe()
    long_before = as_of_for(SCAN_DATE) - timedelta(days=365)

    early = check_readiness(
        connection,
        scan_date=SCAN_DATE,
        as_of=long_before,
        universe_version_id=lineage.universe_version_id,
    )

    assert not early.ready
    assert early.delivered == 0


def test_a_failed_or_pending_run_is_findable_by_status(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """The read a Module 23 observability layer would make."""
    populated_universe(count=5, deliver=1)
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    waiting = runs_by_status(connection, LiveScanStatus.DATA_NOT_READY)

    assert [run.scan_date for run in waiting] == [SCAN_DATE]
    assert runs_by_status(connection, LiveScanStatus.FAILED) == []
