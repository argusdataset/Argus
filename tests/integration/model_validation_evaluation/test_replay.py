"""The full flow: replay -> evaluation -> review gate, against real tables.

This is the first test in ARGUS that drives Modules 06 through 15 end to
end. What it asserts is mostly *shape*: that a replay walks the right
dates, sees the universe as it was on each of them, writes results with
full lineage, and hands the evaluation something it can measure.

What it deliberately does not assert is that ARGUS finds anything. The
fixture universe is a handful of securities with synthetic prices, and
Module 13's bootstrap situation means essentially nothing gets scored
until a real historical scan has populated the case dataset. A test that
demanded signals would be a test that had quietly stubbed out the honesty
of the modules underneath it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection

from core.model_validation_evaluation.evaluation.engine import evaluate
from core.model_validation_evaluation.validation.replay import (
    ModuleConfigs,
    ReplayRefused,
    ReplayRequest,
    ReplayResult,
    replay,
    scan_dates,
)
from core.model_validation_evaluation.validation.review import (
    approve,
    approved_runs,
    is_approved,
    open_for_review,
)
from core.model_validation_evaluation.validation.runs import load_run
from core.scoring.engine import Lineage
from infra.db.enums import ValidationRunStatus
from infra.db.schema.identity import universe_membership
from infra.db.schema.validation import model_evaluation_reports
from infra.db.schema.versioning import feature_vectors
from tests.integration.feature_engine.conftest import insert_bars
from tests.integration.model_validation_evaluation.conftest import (
    AS_OF,
    PERIOD_END,
    PERIOD_START,
)

#: Far enough back to fill Module 08's 252-bar maximum lookback before the
#: replay's first scan date. A shorter history is a legitimate case — it
#: produces honest NaNs — but it would test the empty path rather than the
#: populated one.
HISTORY_START = datetime(2019, 1, 2, tzinfo=UTC)


def _shape(index: int, length: int) -> list[float]:
    """A decline, a base, and an awakening — the pattern ARGUS looks for.

    Deterministic rather than random: a fixture whose feature values move
    between runs makes every downstream assertion a coin flip.
    """
    closes: list[float] = []
    for n in range(length):
        phase = n / length
        if phase < 0.4:
            level = 100.0 - 55.0 * (phase / 0.4)
        elif phase < 0.8:
            level = 45.0 + 2.0 * ((n % 7) - 3) / 3.0
        else:
            level = 45.0 + 40.0 * ((phase - 0.8) / 0.2)
        closes.append(level + index)
    return closes


@pytest.fixture
def populated_universe(connection: Connection, register, lineage: Lineage) -> list[UUID]:
    """Five securities with bars and universe membership across the period."""
    security_ids = []
    import pandas as pd

    length = len(pd.bdate_range(HISTORY_START, PERIOD_END))
    for index, ticker in enumerate(("MLSS", "SLS", "HIVE", "ALX", "QBTS")):
        security_id = register(ticker)
        insert_bars(
            connection,
            security_id,
            start=HISTORY_START,
            closes=_shape(index, length),
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
        security_ids.append(security_id)
    return security_ids


def _request(lineage: Lineage, **overrides) -> ReplayRequest:
    return ReplayRequest(
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        lineage=lineage,
        notes="module17 end-to-end test",
        **overrides,
    )


# --------------------------------------------------------------------------
# Scan dates
# --------------------------------------------------------------------------


def test_the_replay_visits_trading_days_at_the_configured_step():
    dates = scan_dates(PERIOD_START, PERIOD_END)

    assert dates
    assert all(moment.weekday() < 5 for moment in dates)
    assert dates[0] >= PERIOD_START
    assert dates[-1] <= PERIOD_END
    # Weekly by default, so roughly one date per calendar week.
    assert 10 < len(dates) < 20


def test_a_holiday_shifts_a_scan_rather_than_deleting_it():
    """Independence Day 2022 falls on a Monday; the scan lands on the 5th."""
    dates = scan_dates(datetime(2022, 7, 4, tzinfo=UTC), datetime(2022, 7, 20, tzinfo=UTC))

    assert dates[0].date() == datetime(2022, 7, 5, tzinfo=UTC).date()


def test_a_reversed_period_is_refused_rather_than_silently_producing_nothing():
    with pytest.raises(ReplayRefused) as exc_info:
        scan_dates(PERIOD_END, PERIOD_START)
    assert "precedes period_start" in str(exc_info.value)


def test_an_absurd_number_of_scan_dates_is_refused_before_the_run_starts():
    """The caller asked for something other than what they meant."""
    from core.model_validation_evaluation.validation.config import (
        ReplaySettings,
        ValidationConfig,
        ValidationSetting,
    )

    tiny_limit = ValidationConfig(
        settings=ReplaySettings(
            max_scan_dates=ValidationSetting(value=3.0, kind="operational", rationale="test")
        )
    )
    with pytest.raises(ReplayRefused) as exc_info:
        scan_dates(PERIOD_START, PERIOD_END, config=tiny_limit)
    assert "more than 3 scan dates" in str(exc_info.value)


# --------------------------------------------------------------------------
# The replay itself
# --------------------------------------------------------------------------


def test_a_replay_walks_the_period_and_records_a_completed_run(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, populated_universe
):
    result = replay(connection, _request(lineage), modules=modules)

    assert isinstance(result, ReplayResult)
    assert len(result.scan_dates) == len(scan_dates(PERIOD_START, PERIOD_END))

    run = load_run(connection, result.run_id)
    assert run is not None
    assert run.status is ValidationRunStatus.COMPLETED
    assert run.lineage == lineage
    assert run.period_start == PERIOD_START


def test_the_replay_sees_the_universe_as_it_was_on_each_scan_date(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, populated_universe
):
    """Not "who is listed today". Using today's listings would be
    survivorship bias introduced at the one point where it is invisible
    afterwards."""
    result = replay(connection, _request(lineage), modules=modules)

    assert all(scan.universe_size == len(populated_universe) for scan in result.scan_dates)


def test_a_security_listed_only_partway_through_appears_only_from_then_on(
    connection: Connection, register, lineage: Lineage, modules: ModuleConfigs, populated_universe
):
    latecomer = register("LATE")
    midpoint = PERIOD_START + timedelta(days=45)
    connection.execute(
        universe_membership.insert().values(
            universe_version_id=lineage.universe_version_id,
            security_id=latecomer,
            listing_status="LISTED",
            listed_from=midpoint,
            listed_to=None,
            exchange="NASDAQ",
            interval_evidence="reported",
        )
    )

    result = replay(connection, _request(lineage), modules=modules)
    sizes = [scan.universe_size for scan in result.scan_dates]

    assert min(sizes) == len(populated_universe)
    assert max(sizes) == len(populated_universe) + 1
    # And the change happens where the listing does, not at the start.
    assert sizes[0] == len(populated_universe)
    assert sizes[-1] == len(populated_universe) + 1


def test_the_replay_writes_feature_vectors_with_the_lineages_schema_version(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, populated_universe
):
    replay(connection, _request(lineage), modules=modules)

    versions = (
        connection.execute(select(feature_vectors.c.feature_schema_version_id).distinct())
        .scalars()
        .all()
    )

    assert versions == [lineage.feature_schema_version_id]


def test_a_replay_over_a_period_with_no_trading_days_completes_without_writing(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, populated_universe
):
    """Christmas Day 2021 fell on a Saturday; the 24th was a holiday.

    A period containing no scan dates is an empty run, not an error — and
    it still records a run row, because "we looked and there was nothing
    to look at" is worth being able to find later.
    """
    result = replay(
        connection,
        ReplayRequest(
            period_start=datetime(2021, 12, 25, tzinfo=UTC),
            period_end=datetime(2021, 12, 26, tzinfo=UTC),
            lineage=lineage,
        ),
        modules=modules,
    )

    assert result.scan_dates == []
    assert load_run(connection, result.run_id).status is ValidationRunStatus.COMPLETED


def test_a_replay_that_raises_leaves_a_failed_run_naming_what_was_in_flight(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, populated_universe
):
    """A crashed run must be identifiable afterwards.

    Creating the run row on success instead would make a crash
    indistinguishable from a run that never started, and its half-written
    signals indistinguishable from anyone else's.
    """
    from unittest.mock import patch

    with (
        patch(
            "core.model_validation_evaluation.validation.replay.compute_features_in_chunks",
            side_effect=RuntimeError("panel load exploded"),
        ),
        pytest.raises(RuntimeError),
    ):
        replay(connection, _request(lineage), modules=modules)

    run_id = connection.execute(
        text("SELECT id FROM model_validation_runs ORDER BY started_at DESC LIMIT 1")
    ).scalar_one()
    run = load_run(connection, run_id)

    assert run.status is ValidationRunStatus.FAILED
    assert run.period_start == PERIOD_START


# --------------------------------------------------------------------------
# Replay -> evaluation -> review gate
# --------------------------------------------------------------------------


def test_the_whole_flow_produces_a_stored_report_behind_the_review_gate(
    connection: Connection,
    lineage: Lineage,
    modules: ModuleConfigs,
    populated_universe,
    register,
    seed_setup,
):
    """The flow the module exists for, with a seeded population to measure.

    The setups are seeded rather than produced by the replay, and that is
    the honest arrangement: Module 13 scores nothing until the case
    dataset exists, so a replay-produced population would be empty and
    the evaluation would have nothing to compute. The replay's own
    behaviour is asserted above; this asserts that its output flows into
    an evaluation and that the evaluation's numbers stay behind the gate.
    """
    for index in range(4):
        seed_setup(
            register(f"CASE{index}"),
            detected_at=PERIOD_START + timedelta(days=index),
        )

    result = replay(connection, _request(lineage), modules=modules)
    report = evaluate(connection, result.run_id, as_of=AS_OF)

    assert report.report_id is not None
    assert report.overall.sample_size == 4
    stored = connection.execute(
        select(model_evaluation_reports.c.sample_size, model_evaluation_reports.c.metrics).where(
            model_evaluation_reports.c.model_validation_run_id == result.run_id
        )
    ).one()
    assert stored.sample_size == 4
    assert stored.metrics["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"
    assert stored.metrics["dataset"]["setups"] == 4

    # A report exists; the run is not approved, so Module 20 sees nothing.
    assert not is_approved(connection, result.run_id)
    assert result.run_id not in approved_runs(connection)


def test_a_reviewer_can_move_a_completed_run_through_the_gate(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, populated_universe
):
    role_id = connection.execute(
        text("INSERT INTO roles (name) VALUES ('module17-flow') RETURNING id")
    ).scalar_one()
    reviewer = connection.execute(
        text(
            "INSERT INTO users (email, display_name, role_id) "
            "VALUES ('flow@example.test', 'Flow Reviewer', :role) RETURNING id"
        ),
        {"role": role_id},
    ).scalar_one()

    result = replay(connection, _request(lineage), modules=modules)
    evaluate(connection, result.run_id, as_of=AS_OF)

    open_for_review(connection, result.run_id, note="ready for review")
    assert not is_approved(connection, result.run_id)

    approve(connection, result.run_id, user_id=reviewer, note="checked")
    assert is_approved(connection, result.run_id)
    assert result.run_id in approved_runs(connection)


def test_an_evaluation_of_an_unknown_run_is_refused(connection: Connection):
    from uuid import uuid4

    from core.model_validation_evaluation.evaluation.engine import EvaluationRefused

    with pytest.raises(EvaluationRefused):
        evaluate(connection, uuid4(), as_of=AS_OF)


def test_the_evaluation_is_bounded_by_the_runs_own_period(
    connection: Connection,
    lineage: Lineage,
    modules: ModuleConfigs,
    populated_universe,
    register,
    seed_setup,
):
    """A run cannot be measured against setups from outside what it replayed."""
    seed_setup(register("INSIDE"), detected_at=PERIOD_START + timedelta(days=5))
    seed_setup(register("OUTSIDE"), detected_at=PERIOD_END + timedelta(days=400))

    result = replay(connection, _request(lineage), modules=modules)
    report = evaluate(connection, result.run_id, as_of=AS_OF + timedelta(days=800))

    assert report.overall.sample_size == 1


def test_the_replay_result_serializes_its_whole_lineage_and_consistency_check(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, populated_universe
):
    """A run nobody can reconstruct is a run nobody can trust."""
    result = replay(connection, _request(lineage), modules=modules)
    payload = result.as_dict()

    assert payload["target_model_version_id"] == str(lineage.target_model_version_id)
    assert payload["consistency"]["consistent"] is True
    assert payload["intent"] == "REPLAY"
    assert payload["totals"]["scan_dates"] == len(result.scan_dates)


def test_two_replays_of_the_same_period_do_not_double_count_anything(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, populated_universe
):
    """Re-running is idempotent at the storage layer.

    Every writer downstream inserts with `ON CONFLICT DO NOTHING` against
    an identity key. If that ever stopped holding, a re-run would silently
    double every count in the evaluation.
    """
    replay(connection, _request(lineage), modules=modules)
    first = connection.execute(select(func.count()).select_from(feature_vectors)).scalar_one()

    replay(connection, _request(lineage), modules=modules)
    second = connection.execute(select(func.count()).select_from(feature_vectors)).scalar_one()

    assert second == first
