"""Running twice must not mean twice the data.

A batch replay runs once, deliberately, with a person watching. A daily
scanner runs twice all the time — a retry after a blip, a scheduler
double-fire, a manual re-trigger to catch up after downtime. So the
question is not whether duplicate protection exists somewhere; it is
whether it holds for the pattern this module actually produces.

Two layers, tested separately because they fail differently:

- **The scanner refuses to re-run a completed date.** This is the promise:
  a scan runs once.
- **The storage layer would absorb it anyway.** Every writer downstream
  inserts against an identity key. This is the safety net, and it is
  tested by forcing a re-run past the first layer.

A system with only the second layer is correct and wasteful. One with only
the first is fast and fragile. Both is the point.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from core.live_scanner.results import scan_results
from core.live_scanner.runs import completed_dates, latest_run, run_history
from core.live_scanner.scanner import run_scan
from core.live_scanner.schedule import as_of_for
from core.scoring.engine import Lineage
from infra.db.enums import LiveScanStatus
from infra.db.schema.intelligence import signals
from infra.db.schema.live_scanner import live_scan_runs
from infra.db.schema.setups import setup_events, setups
from infra.db.schema.versioning import feature_vectors
from tests.integration.live_scanner.conftest import SCAN_DATE


def _counts(connection: Connection) -> dict[str, int]:
    """Every table a scan writes to, counted."""
    return {
        table.name: int(connection.execute(select(func.count()).select_from(table)).scalar_one())
        for table in (feature_vectors, signals, setups, setup_events)
    }


def test_running_the_same_date_twice_scans_once(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    populated_universe()
    first = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)
    second = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    assert first.succeeded and not first.skipped
    assert second.succeeded and second.skipped
    assert "Already scanned on attempt 0" in second.reason
    assert len(run_history(connection, SCAN_DATE)) == 1


def test_the_second_run_writes_nothing_at_all(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """The check is at the scanner, so 'runs once' is a property of the
    scanner rather than a side effect of unique indexes."""
    populated_universe()
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)
    before = _counts(connection)

    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    assert _counts(connection) == before


def test_forcing_a_re_run_still_produces_no_duplicates(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """The safety net, tested by deliberately defeating the first layer.

    `force=True` puts the scan through the whole pipeline a second time
    for the same `as_of`. Every downstream writer inserts against an
    identity key — Module 08's feature vectors, Module 13's partial unique
    index on signals, Module 14's one-open-setup-per-security — so the
    stored state must come out identical.
    """
    populated_universe()
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)
    before = _counts(connection)

    forced = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, force=True)

    assert forced.succeeded
    assert not forced.skipped
    assert _counts(connection) == before, "a forced re-scan duplicated stored rows"


def test_a_forced_re_run_records_a_second_attempt_rather_than_editing_the_first(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """The run history is the one thing that legitimately grows."""
    populated_universe()
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, force=True)

    history = run_history(connection, SCAN_DATE)

    assert [run.attempt for run in history] == [0, 1]
    assert all(run.status is LiveScanStatus.COMPLETED for run in history)
    assert latest_run(connection, SCAN_DATE).attempt == 1


def test_a_second_attempt_cannot_claim_the_same_attempt_number(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """What makes the read-then-write in `next_attempt` safe.

    Two schedulers firing at once both read the same maximum; the database
    refuses one of them rather than silently storing two rows that claim
    to be the same attempt.
    """
    import pytest
    from sqlalchemy.exc import IntegrityError

    populated_universe()
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    with pytest.raises(IntegrityError) as exc_info:
        connection.execute(
            live_scan_runs.insert().values(
                scan_date=SCAN_DATE,
                as_of=as_of_for(SCAN_DATE),
                attempt=0,
                status=LiveScanStatus.RUNNING.value,
                target_model_version_id=lineage.target_model_version_id,
                feature_schema_version_id=lineage.feature_schema_version_id,
                scoring_configuration_id=lineage.scoring_configuration_id,
                detection_configuration_id=lineage.detection_configuration_id,
                universe_version_id=lineage.universe_version_id,
                data_snapshot_id=lineage.data_snapshot_id,
            )
        )
    assert "uq_live_scan_date_attempt" in str(exc_info.value)


def test_a_re_scan_produces_the_same_reported_results(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """Not just the same row counts — the same answer to the question a
    consumer asks."""
    populated_universe()
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)
    first = scan_results(connection, SCAN_DATE).as_dict()

    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, force=True)
    second = scan_results(connection, SCAN_DATE).as_dict()

    for key in ("signals_written", "scored_signals", "setups_opened", "decisions"):
        assert first[key] == second[key]


def test_only_successful_runs_count_as_done(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """A date whose last attempt was DATA_NOT_READY is not done and must
    come back around. Treating "we tried" as "we finished" is how a
    scanner skips a day and never notices."""
    populated_universe(count=5, deliver=1)
    outcome = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    assert outcome.status is LiveScanStatus.DATA_NOT_READY
    assert completed_dates(connection) == set()


def test_a_date_that_failed_then_succeeded_counts_as_done(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """Any successful attempt settles the date — a day that failed twice
    and then worked is finished, not permanently outstanding."""
    populated_universe(count=5, deliver=1)
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)
    assert completed_dates(connection) == set()

    # Enough fully-delivered names arrive that the universe clears the
    # 80% coverage floor: 21 of 25 rather than 6 of 10.
    populated_universe(count=20, prefix="LATE", deliver=20)
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    assert completed_dates(connection) == {SCAN_DATE}


def test_completed_dates_can_be_bounded_to_a_window(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    populated_universe()
    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    assert completed_dates(connection, since=SCAN_DATE) == {SCAN_DATE}
    assert completed_dates(connection, until=date(2021, 1, 1)) == set()
