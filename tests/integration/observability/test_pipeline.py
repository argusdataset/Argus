"""Live-scan health, read through Module 18's feed rather than around it."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

from infra.db.enums import LiveScanStatus
from infra.observability.config import ObservabilitySettings
from infra.observability.pipeline import ingestion_health, scan_health, stuck_runs
from tests.integration.observability.conftest import NOW

TODAY = NOW.date()


# --------------------------------------------------------------------------
# Consuming, not recomputing
# --------------------------------------------------------------------------


def test_this_module_never_queries_live_scan_runs_itself():
    """The discipline, held structurally rather than by intention.

    Module 18 owns what a scan status means. A monitor that queried the
    table directly would grow its own opinion, and the first time the two
    disagreed nobody would know which was right. Checked on the parse tree
    — a text scan passes against a renamed import.
    """
    tree = ast.parse(Path("infra/observability/pipeline.py").read_text())

    imported = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }

    assert ("core.live_scanner.runs", "runs_by_status") in imported
    assert not any(name == "live_scan_runs" for _module, name in imported), (
        "pipeline.py imports the scan-runs table and should read Module 18's feed"
    )


def test_scan_health_reports_a_count_for_every_status(connection, add_scan_run):
    """Every status present even at zero — a missing key is not a zero."""
    add_scan_run(scan_date=TODAY, status=LiveScanStatus.COMPLETED, finished_at=NOW)

    health = scan_health(connection, now=NOW)

    assert set(health.counts) == {status.value for status in LiveScanStatus}
    assert health.counts["COMPLETED"] == 1
    assert health.counts["FAILED"] == 0


def test_a_failed_run_makes_the_pipeline_unhealthy(connection, add_scan_run):
    """Module 18's taxonomy: FAILED wants a person."""
    add_scan_run(
        scan_date=TODAY, status=LiveScanStatus.FAILED, finished_at=NOW, note="provider 500"
    )

    health = scan_health(connection, now=NOW)

    assert health.healthy is False
    assert len(health.failed) == 1
    assert health.failed[0]["note"] == "provider 500"


def test_data_not_ready_is_patience_and_not_an_incident(connection, add_scan_run):
    """Module 18's words, honoured: "DATA_NOT_READY wants patience"."""
    add_scan_run(scan_date=TODAY, status=LiveScanStatus.DATA_NOT_READY)

    health = scan_health(connection, now=NOW)

    assert health.healthy is True
    assert health.counts["DATA_NOT_READY"] == 1


def test_exclusions_alone_do_not_make_the_pipeline_unhealthy(connection, add_scan_run, uid):
    """ "COMPLETED_WITH_EXCLUSIONS wants a look", which is not an alert."""
    add_scan_run(
        scan_date=TODAY,
        status=LiveScanStatus.COMPLETED_WITH_EXCLUSIONS,
        finished_at=NOW,
        excluded=[uid()],
    )

    assert scan_health(connection, now=NOW).healthy is True


# --------------------------------------------------------------------------
# The two cross-run questions a single run cannot answer
# --------------------------------------------------------------------------


def test_a_running_row_past_the_window_is_a_died_process(connection, add_scan_run):
    """Module 18's sentence with a number attached.

    A run's own report cannot make this call: the process that would have
    written the conclusion is the one that died.
    """
    add_scan_run(
        scan_date=(NOW - timedelta(days=2)).date(),
        status=LiveScanStatus.RUNNING,
        as_of=NOW - timedelta(days=2),
        finished_at=None,
    )

    health = scan_health(connection, now=NOW)

    assert health.healthy is False
    assert len(health.stuck) == 1
    assert health.stuck[0]["finished_at"] is None


def test_a_running_row_inside_the_window_is_just_running(connection, add_scan_run):
    """A scan in progress is not a scan that died."""
    add_scan_run(
        scan_date=TODAY,
        status=LiveScanStatus.RUNNING,
        as_of=NOW - timedelta(minutes=20),
        finished_at=None,
    )

    health = scan_health(connection, now=NOW)

    assert health.stuck == []
    assert health.healthy is True


def test_the_stuck_rule_is_pure_and_testable_without_a_database(connection, add_scan_run):
    """So the rule can be argued about without standing up Postgres."""
    from core.live_scanner.runs import runs_by_status

    add_scan_run(
        scan_date=(NOW - timedelta(days=1)).date(),
        status=LiveScanStatus.RUNNING,
        as_of=NOW - timedelta(days=1),
    )
    running = runs_by_status(connection, LiveScanStatus.RUNNING)

    assert stuck_runs(running, now=NOW) != []
    # An hour after the session it was scanning, nothing is wrong yet.
    assert stuck_runs(running, now=NOW - timedelta(days=1) + timedelta(hours=1)) == []


def test_a_security_excluded_on_several_days_is_surfaced(connection, add_scan_run, uid):
    """ "wants a look if the same security appears every day".

    The failure that hides inside a green dashboard: a security ARGUS is
    quietly never looking at, while every run reports success.
    """
    repeat, once = uid(), uid()
    for offset in range(3):
        add_scan_run(
            scan_date=TODAY - timedelta(days=offset),
            status=LiveScanStatus.COMPLETED_WITH_EXCLUSIONS,
            finished_at=NOW,
            excluded=[repeat] + ([once] if offset == 0 else []),
        )

    health = scan_health(connection, now=NOW)

    assert [item["security_id"] for item in health.persistent_exclusions] == [repeat]
    assert health.persistent_exclusions[0]["days"] == 3
    assert len(health.persistent_exclusions[0]["scan_dates"]) == 3


def test_a_one_off_exclusion_is_not_surfaced(connection, add_scan_run, uid):
    """A security excluded once is a bad day, not a pattern."""
    add_scan_run(
        scan_date=TODAY,
        status=LiveScanStatus.COMPLETED_WITH_EXCLUSIONS,
        finished_at=NOW,
        excluded=[uid()],
    )

    assert scan_health(connection, now=NOW).persistent_exclusions == []


def test_the_report_serialises_to_something_a_tool_can_read(connection, add_scan_run):
    add_scan_run(scan_date=TODAY, status=LiveScanStatus.COMPLETED, finished_at=NOW)

    payload = scan_health(connection, now=NOW).as_dict()

    assert set(payload) == {
        "as_of",
        "healthy",
        "counts",
        "failed",
        "stuck",
        "persistent_exclusions",
    }


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------


def test_ingestion_lag_is_measured_from_recorded_provenance(connection, register, add_bar):
    """No provider is contacted; the answer is on disk already."""
    security_id = register("LAG")
    for hours in (2, 4, 6):
        add_bar(
            security_id,
            event_time=NOW - timedelta(days=1),
            available_at=NOW - timedelta(days=1) + timedelta(hours=hours),
        )

    lags = {item.feed: item for item in ingestion_health(connection, now=NOW)}

    assert lags["ohlcv"].rows == 3
    assert lags["ohlcv"].median_lag_seconds == 4 * 3600
    assert lags["ohlcv"].max_lag_seconds == 6 * 3600


def test_a_feed_with_no_rows_reports_no_lag_rather_than_zero(connection):
    """Zero would read as "instant", which is the opposite of unknown."""
    lags = {item.feed: item for item in ingestion_health(connection, now=NOW)}

    assert lags["news"].rows == 0
    assert lags["news"].median_lag_seconds is None
    assert lags["news"].max_lag_seconds is None


def test_ingestion_health_bounds_itself_to_a_window(connection, register, add_bar):
    """Old rows do not drag the current lag around forever."""
    security_id = register("WINDOWED")
    add_bar(
        security_id,
        event_time=NOW - timedelta(days=400),
        available_at=NOW - timedelta(days=400) + timedelta(days=5),
    )
    add_bar(
        security_id,
        event_time=NOW - timedelta(days=1),
        available_at=NOW - timedelta(days=1) + timedelta(hours=1),
    )

    settings = ObservabilitySettings()
    lags = {item.feed: item for item in ingestion_health(connection, now=NOW, settings=settings)}

    assert lags["ohlcv"].rows == 1, "only the row inside the window is counted"
    assert lags["ohlcv"].max_lag_seconds == 3600
