"""The failure policy: three kinds of wrong, three responses, nothing silent.

Module 17's batch replay records FAILED and re-raises. That is right when
a person started the run and will re-run it. Unattended, it means the
process crashes and nobody knows until somebody happens to look — so every
test here is about the scanner *not* doing that.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import Connection

from core.live_scanner.config import ScannerConfig, ScannerSetting, ScannerSettings
from core.live_scanner.runs import latest_run, run_history
from core.live_scanner.scanner import isolate_failing_securities, run_scan
from core.live_scanner.schedule import as_of_for
from core.market_state.thresholds import MAGNITUDE, MarketStateConfig, StateThresholds, Threshold
from core.scoring.engine import Lineage
from infra.db.enums import LiveScanStatus
from tests.integration.live_scanner.conftest import SCAN_DATE

SCAN_PATH = "core.live_scanner.scanner.scan_one_date"


def _dbapi_error() -> sa_exc.OperationalError:
    return sa_exc.OperationalError("SELECT 1", {}, Exception("server closed the connection"))


# --------------------------------------------------------------------------
# Transient
# --------------------------------------------------------------------------


def test_a_transient_failure_is_retried_and_the_day_still_completes(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """A dropped connection must not cost a trading day.

    The first two attempts die the way a database restart looks; the third
    is the real call. Batch's policy would have raised on the first.
    """
    populated_universe()
    real = __import__(
        "core.model_validation_evaluation.validation.replay", fromlist=["scan_one_date"]
    ).scan_one_date
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _dbapi_error()
        return real(*args, **kwargs)

    with patch(SCAN_PATH, side_effect=flaky):
        outcome = run_scan(
            connect,
            scan_date=SCAN_DATE,
            lineage=lineage,
            modules=modules,
            sleep=no_sleep,
        )

    assert outcome.status is LiveScanStatus.COMPLETED
    assert outcome.attempts == 3
    assert calls["n"] == 3


def test_the_retries_back_off_rather_than_hammering(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    populated_universe()
    with patch(SCAN_PATH, side_effect=_dbapi_error()):
        run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, sleep=no_sleep)

    waits = no_sleep.waits
    assert waits == sorted(waits)
    assert waits == [2.0, 4.0, 8.0]


def test_each_failed_attempt_leaves_its_own_row_so_the_evening_is_recoverable(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """A date that took four tries leaves four rows. That is the question
    anyone investigating a flaky scanner actually asks."""
    populated_universe()
    with patch(SCAN_PATH, side_effect=_dbapi_error()):
        run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, sleep=no_sleep)

    history = run_history(connection, SCAN_DATE)

    assert [run.attempt for run in history] == [0, 1, 2, 3]
    assert all(run.status is LiveScanStatus.FAILED for run in history)
    assert "retrying" in history[0].note
    assert "Retries exhausted" in history[-1].note


def test_exhausted_retries_record_failed_and_do_not_raise(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """The whole difference from batch. An unattended process that raises
    crashes, and a crashed scanner is one nobody hears from."""
    populated_universe()
    with patch(SCAN_PATH, side_effect=_dbapi_error()):
        outcome = run_scan(
            connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, sleep=no_sleep
        )

    assert outcome.status is LiveScanStatus.FAILED
    assert not outcome.succeeded
    stored = latest_run(connection, SCAN_DATE)
    assert stored.status is LiveScanStatus.FAILED
    assert stored.detail["failure"]["classification"] == "TRANSIENT"
    assert "next scheduled run will try this date again" in stored.note


def test_the_number_of_attempts_is_configuration(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    populated_universe()
    once = ScannerConfig(
        settings=ScannerSettings(
            max_attempts=ScannerSetting(value=1.0, kind="operational", rationale="test")
        )
    )

    with patch(SCAN_PATH, side_effect=_dbapi_error()):
        outcome = run_scan(
            connect,
            scan_date=SCAN_DATE,
            lineage=lineage,
            modules=modules,
            config=once,
            sleep=no_sleep,
        )

    assert outcome.attempts == 1
    assert no_sleep.waits == []


# --------------------------------------------------------------------------
# Isolatable
# --------------------------------------------------------------------------


def test_one_bad_security_is_quarantined_and_the_rest_of_the_day_completes(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """Failing a whole day for one bad name means one broken row stops all
    of ARGUS — and since the row will still be there tomorrow, it stops it
    permanently."""
    securities = populated_universe(count=10)
    poisoned = securities[3]

    real_chunks = __import__(
        "core.model_validation_evaluation.validation.replay",
        fromlist=["compute_features_in_chunks"],
    ).compute_features_in_chunks

    def explode_on_poisoned(connection_, security_ids, **kwargs):
        if poisoned in security_ids:
            raise ValueError(f"malformed bar series for {poisoned}")
        return real_chunks(connection_, security_ids, **kwargs)

    with (
        patch(
            "core.model_validation_evaluation.validation.replay.compute_features_in_chunks",
            side_effect=explode_on_poisoned,
        ),
        patch(
            "core.live_scanner.scanner.compute_features_in_chunks",
            side_effect=explode_on_poisoned,
        ),
    ):
        outcome = run_scan(
            connect,
            scan_date=SCAN_DATE,
            lineage=lineage,
            modules=modules,
            sleep=no_sleep,
        )

    assert outcome.status is LiveScanStatus.COMPLETED_WITH_EXCLUSIONS
    assert outcome.succeeded
    assert [entry.security_id for entry in outcome.excluded] == [poisoned]
    assert outcome.result.universe_size == 9


def test_the_quarantine_is_recorded_with_the_reason_not_just_a_count(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """ "Which name has been failing every day this week" is the question
    that makes per-security isolation worth having, and a count cannot
    answer it."""
    securities = populated_universe(count=10)
    poisoned = securities[3]

    real_chunks = __import__(
        "core.model_validation_evaluation.validation.replay",
        fromlist=["compute_features_in_chunks"],
    ).compute_features_in_chunks

    def explode(connection_, security_ids, **kwargs):
        if poisoned in security_ids:
            raise ValueError("malformed bar series")
        return real_chunks(connection_, security_ids, **kwargs)

    with (
        patch(
            "core.model_validation_evaluation.validation.replay.compute_features_in_chunks",
            side_effect=explode,
        ),
        patch("core.live_scanner.scanner.compute_features_in_chunks", side_effect=explode),
    ):
        run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, sleep=no_sleep)

    stored = latest_run(connection, SCAN_DATE)

    assert stored.status is LiveScanStatus.COMPLETED_WITH_EXCLUSIONS
    assert stored.excluded_count == 1
    entry = stored.excluded_securities[0]
    assert entry["security_id"] == str(poisoned)
    assert entry["error_type"] == "ValueError"
    assert "malformed bar series" in entry["reason"]


def test_too_many_exclusions_is_a_failure_rather_than_a_scan_with_caveats(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """Isolating one bad name to save the day is the point. Isolating a
    third of the universe and reporting success is how a broken feed gets
    filed as a normal Tuesday."""
    securities = populated_universe(count=10)
    broken = set(securities[:6])

    real_chunks = __import__(
        "core.model_validation_evaluation.validation.replay",
        fromlist=["compute_features_in_chunks"],
    ).compute_features_in_chunks

    def explode(connection_, security_ids, **kwargs):
        if broken & set(security_ids):
            raise ValueError("feed is broken")
        return real_chunks(connection_, security_ids, **kwargs)

    with (
        patch(
            "core.model_validation_evaluation.validation.replay.compute_features_in_chunks",
            side_effect=explode,
        ),
        patch("core.live_scanner.scanner.compute_features_in_chunks", side_effect=explode),
    ):
        outcome = run_scan(
            connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, sleep=no_sleep
        )

    assert outcome.status is LiveScanStatus.FAILED
    assert "60.0%" in outcome.reason
    assert "broken feed rather than a few bad names" in outcome.reason


def test_a_failure_belonging_to_no_single_security_says_so(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """The probe's honest limitation, reported rather than hidden.

    A failure arising only from cross-sectional code belongs to no one
    security, so probing finds nothing — and the recorded reason points at
    the scan as a whole rather than leaving someone hunting for bad data.
    """
    populated_universe(count=6)

    with patch(SCAN_PATH, side_effect=ValueError("ranking blew up over the whole pool")):
        outcome = run_scan(
            connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, sleep=no_sleep
        )

    assert outcome.status is LiveScanStatus.FAILED
    assert outcome.excluded == []
    assert "could not be attributed to any single security" in outcome.reason
    assert "cross-sectional code" in outcome.reason


def test_the_isolation_probe_runs_the_same_code_the_scan_does(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """A chunk of one, which Module 17 proved is result-identical to any
    other chunk size — so this is the same code path, not a cheaper
    approximation of it."""
    securities = populated_universe(count=4)

    with connect() as conn:
        clean = isolate_failing_securities(
            conn,
            securities,
            as_of=as_of_for(SCAN_DATE),
            lineage=lineage,
            modules=modules,
        )

    assert clean == []


def test_the_probe_reports_only_the_securities_that_actually_throw(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    securities = populated_universe(count=4)
    poisoned = securities[1]

    real_chunks = __import__(
        "core.model_validation_evaluation.validation.replay",
        fromlist=["compute_features_in_chunks"],
    ).compute_features_in_chunks

    def explode(connection_, security_ids, **kwargs):
        if poisoned in security_ids:
            raise KeyError("close_adj")
        return real_chunks(connection_, security_ids, **kwargs)

    with (
        patch("core.live_scanner.scanner.compute_features_in_chunks", side_effect=explode),
        connect() as conn,
    ):
        failures = isolate_failing_securities(
            conn,
            securities,
            as_of=as_of_for(SCAN_DATE),
            lineage=lineage,
            modules=modules,
        )

    assert [entry.security_id for entry in failures] == [poisoned]
    assert failures[0].error_type == "KeyError"


# --------------------------------------------------------------------------
# Permanent
# --------------------------------------------------------------------------


def test_a_version_mismatch_fails_immediately_without_retrying(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """Correction 3, enforced on a live scan.

    A deploy happens between scans and nobody re-reads the lineage
    afterwards, so this is where a silent version drift is *most* likely
    to go unnoticed. Retrying it would be pointless — the fix is to
    publish a version or check out different code.
    """
    populated_universe()
    drifted = replace(
        modules,
        market_state=MarketStateConfig(
            states=StateThresholds(
                downtrend_lower_low_frequency=Threshold(
                    value=0.99, kind=MAGNITUDE, rationale="stands in for a code change"
                )
            )
        ),
    )

    outcome = run_scan(
        connect,
        scan_date=SCAN_DATE,
        lineage=lineage,
        modules=drifted,
        sleep=no_sleep,
    )

    assert outcome.status is LiveScanStatus.FAILED
    assert outcome.attempts == 1
    assert no_sleep.waits == []
    stored = latest_run(connection, SCAN_DATE)
    assert stored.detail["failure"]["classification"] == "PERMANENT"
    assert "VersionMismatch" in stored.detail["failure"]["type"]


def test_a_scan_refused_on_versions_writes_no_signals(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """The refusal has to come before the work, not after."""
    from sqlalchemy import func, select

    from infra.db.schema.versioning import feature_vectors

    populated_universe()
    drifted = replace(
        modules,
        market_state=MarketStateConfig(
            states=StateThresholds(
                downtrend_lower_low_frequency=Threshold(
                    value=0.99, kind=MAGNITUDE, rationale="stands in for a code change"
                )
            )
        ),
    )

    run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=drifted, sleep=no_sleep)

    written = connection.execute(select(func.count()).select_from(feature_vectors)).scalar_one()
    assert written == 0


def test_a_constraint_violation_is_not_retried(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """It will be violated identically on every attempt."""
    populated_universe()
    violation = sa_exc.IntegrityError("INSERT", {}, Exception("duplicate key"))

    with patch(SCAN_PATH, side_effect=violation):
        outcome = run_scan(
            connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, sleep=no_sleep
        )

    assert outcome.status is LiveScanStatus.FAILED
    assert outcome.attempts == 1
    assert no_sleep.waits == []


def test_a_failed_scan_leaves_the_date_outstanding_for_the_next_run(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """FAILED is not "done". The next scheduled run must pick the date up
    again rather than treating the failure as a decision."""
    from core.live_scanner.runs import completed_dates

    populated_universe()
    with patch(SCAN_PATH, side_effect=_dbapi_error()):
        run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, sleep=no_sleep)

    assert completed_dates(connection) == set()

    outcome = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)
    assert outcome.status is LiveScanStatus.COMPLETED
    assert not outcome.skipped


@pytest.mark.parametrize(
    "error", [ValueError("bad data"), _dbapi_error(), RuntimeError("who knows")]
)
def test_no_failure_ever_escapes_as_an_exception(
    connect, lineage: Lineage, modules, populated_universe, no_sleep, error
):
    """The single most important property of an unattended process."""
    populated_universe()
    with patch(SCAN_PATH, side_effect=error):
        outcome = run_scan(
            connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, sleep=no_sleep
        )

    assert outcome.status is LiveScanStatus.FAILED


# --------------------------------------------------------------------------
# The crash case — the reason the run row is committed before the work
# --------------------------------------------------------------------------


def test_a_process_killed_mid_scan_leaves_a_running_row_naming_the_date(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """The property the three-transaction split exists for.

    `KeyboardInterrupt` is a `BaseException`, so it goes straight past the
    scanner's handling the way a `kill` or an OOM would. The run row was
    committed before the scan's own transaction opened, so it survives —
    and afterwards the date is identifiable as "was in flight when
    something stopped".

    Had the row been written inside the scan's transaction, the rollback
    would take it with it, and a hard failure would be indistinguishable
    from a scan that never started.
    """
    populated_universe()

    with (
        patch(SCAN_PATH, side_effect=KeyboardInterrupt()),
        pytest.raises(KeyboardInterrupt),
    ):
        run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    stranded = latest_run(connection, SCAN_DATE)

    assert stranded is not None
    assert stranded.status is LiveScanStatus.RUNNING
    assert stranded.finished_at is None
    assert stranded.scan_date == SCAN_DATE


def test_a_stranded_running_row_does_not_count_the_date_as_done(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe
):
    """RUNNING is reserved for "in flight, or the process died holding it"
    — the one state that means somebody should look. It must not also mean
    "finished", or a crashed scan would silently consume its date."""
    from core.live_scanner.runs import completed_dates

    populated_universe()
    with (
        patch(SCAN_PATH, side_effect=KeyboardInterrupt()),
        pytest.raises(KeyboardInterrupt),
    ):
        run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)

    assert completed_dates(connection) == set()

    recovered = run_scan(connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules)
    assert recovered.status is LiveScanStatus.COMPLETED
    assert recovered.run.attempt == 1


def test_a_failed_scans_partial_writes_are_discarded(
    connect, connection: Connection, lineage: Lineage, modules, populated_universe, no_sleep
):
    """The other half of the split: the scan's own work is one
    transaction, so a failure leaves no half-written day behind."""
    from sqlalchemy import func, select

    from infra.db.schema.versioning import feature_vectors

    populated_universe()

    real = __import__(
        "core.model_validation_evaluation.validation.replay", fromlist=["scan_one_date"]
    ).scan_one_date

    def write_then_die(connection_, *, as_of, **kwargs):
        real(connection_, as_of=as_of, **kwargs)
        raise ValueError("died after writing the day")

    with patch(SCAN_PATH, side_effect=write_then_die):
        outcome = run_scan(
            connect, scan_date=SCAN_DATE, lineage=lineage, modules=modules, sleep=no_sleep
        )

    assert outcome.status is LiveScanStatus.FAILED
    written = connection.execute(
        select(func.count()).select_from(feature_vectors)
    ).scalar_one()
    assert written == 0, "a failed scan left partial results behind"
