"""The evaluation report against real tables, including what it declines to say.

The unit tests prove the analyses compute correctly against constructed
frames. These prove the frames come out of the database the way the
analyses expect — in particular that migration 0007's `qualifying_signal_id`
really does connect a stored `signals` row to a stored outcome, which is
the join Module 15 could not make and this whole module depends on.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection

from core.model_validation_evaluation.evaluation.config import (
    EvaluationConfig,
    EvaluationThreshold,
    EvaluationThresholds,
)
from core.model_validation_evaluation.evaluation.dataset import load_evaluation_dataset
from core.model_validation_evaluation.evaluation.engine import build_report, evaluate
from core.model_validation_evaluation.validation.runs import finish_run, start_run
from core.scoring.engine import Lineage, score_candidate
from core.scoring.persistence import write_signal
from infra.db.enums import MarketState, OutcomeStatus, ValidationRunStatus
from tests.integration.model_validation_evaluation.conftest import (
    AS_OF,
    PERIOD_END,
    PERIOD_START,
)
from tests.unit.scoring.factories import adequate, scoring_inputs


def _low_floor(minimum: float = 3.0) -> EvaluationConfig:
    return EvaluationConfig(
        thresholds=EvaluationThresholds(
            min_bucket_sample=EvaluationThreshold(
                value=minimum, kind="calibratable", rationale="test"
            ),
            min_regime_sample=EvaluationThreshold(
                value=minimum, kind="calibratable", rationale="test"
            ),
        )
    )


def _report(connection: Connection, run, *, config: EvaluationConfig):
    dataset = load_evaluation_dataset(
        connection,
        as_of=AS_OF,
        period_start=run.period_start,
        period_end=run.period_end,
    )
    return build_report(
        dataset,
        run_id=run.id,
        period_start=run.period_start,
        period_end=run.period_end,
        config=config,
    )


@pytest.fixture
def completed_run(connection: Connection, lineage: Lineage):
    run = start_run(
        connection,
        lineage=lineage,
        period_start=PERIOD_START,
        period_end=PERIOD_END + timedelta(days=365),
    )
    finish_run(connection, run.id, status=ValidationRunStatus.COMPLETED)
    return run


def test_a_stored_score_reaches_the_evaluation_through_the_0007_join_key(
    connection: Connection, register, seed_setup, lineage: Lineage
):
    """The payoff of Correction 2.

    Before migration 0007 the only route from a setup to its score was a
    copy of the number in a lifecycle event's JSONB payload. This asserts
    the real join: a `signals` row, referenced by FK, read back as
    `argus_score` on the evaluation frame.
    """
    security_id = register("JOINED")
    signal = score_candidate(
        scoring_inputs(security_id, cross=adequate()),
        as_of=PERIOD_START,
        lineage=lineage,
    )
    signal_id = write_signal(connection, signal)
    seed_setup(security_id, qualifying_signal_id=signal_id)

    dataset = load_evaluation_dataset(connection, as_of=AS_OF)

    assert len(dataset) == 1
    row = dataset.frame.iloc[0]
    assert row["qualifying_signal_id"] == signal_id
    # `signals.argus_score` is Numeric with a fixed scale, so the stored
    # value is the computed one rounded to the column's precision. Asserted
    # loosely on purpose: the column's rounding is Module 03's decision and
    # a test that demanded exactness here would be testing the wrong thing.
    assert row["argus_score"] == pytest.approx(signal.argus_score, abs=1e-3)
    assert row["confidence"] == pytest.approx(signal.confidence, abs=1e-3)


def test_an_unqualified_setup_is_loaded_with_a_null_score_not_dropped(
    connection: Connection, register, seed_setup
):
    """These rows are recall's denominator and every true negative.

    A LEFT JOIN that became an INNER JOIN would silently produce a system
    measurable only on the cases it already believed in — and precision
    would look fine.
    """
    seed_setup(register("UNQUAL"))
    dataset = load_evaluation_dataset(connection, as_of=AS_OF)

    assert len(dataset) == 1
    assert len(dataset.qualified) == 0
    assert len(dataset.unqualified) == 1


def test_the_dataset_takes_the_latest_labelling_and_records_which(
    connection: Connection, register, seed_setup, lineage: Lineage, second_snapshot: UUID
):
    """Since 0007 a setup may carry several outcomes, one per snapshot."""
    from infra.db.schema.setups import setup_outcomes

    security_id = register("RELABEL")
    setup_id = seed_setup(security_id, outcome_status=OutcomeStatus.FAILED)
    connection.execute(
        setup_outcomes.insert().values(
            setup_id=setup_id,
            outcome_status=OutcomeStatus.SUCCESS.value,
            benchmark_relative_return=0.30,
            data_snapshot_id=second_snapshot,
            recorded_at=AS_OF,
        )
    )

    latest = load_evaluation_dataset(connection, as_of=AS_OF + timedelta(days=1))
    assert latest.frame.iloc[0]["outcome_status"] == "SUCCESS"

    # And a caller may still ask for a specific labelling by name.
    original = load_evaluation_dataset(
        connection, as_of=AS_OF + timedelta(days=1), data_snapshot_id=lineage.data_snapshot_id
    )
    assert original.frame.iloc[0]["outcome_status"] == "FAILED"
    assert original.bounds()["data_snapshot_id"] == str(lineage.data_snapshot_id)


def test_an_outcome_recorded_after_as_of_is_invisible(connection: Connection, register, seed_setup):
    """The PIT cutoff. Scoring the model against knowledge it did not have
    is the exact failure Module 07 exists to prevent."""
    seed_setup(register("FUTURE"), detected_at=PERIOD_START, concluded_at=AS_OF)

    before = load_evaluation_dataset(connection, as_of=PERIOD_START)
    after = load_evaluation_dataset(connection, as_of=AS_OF + timedelta(days=2))

    assert len(before) == 0
    assert len(after) == 1


def test_a_real_report_carries_monotonicity_and_walk_forward_verdicts(
    connection: Connection, register, seed_setup, completed_run
):
    """The full report shape, from real rows.

    Six setups is far below any honest sample floor, so both analyses come
    back undetermined — which is the correct output for this population
    and the one ARGUS will produce until a real scan has run.
    """
    for index in range(6):
        seed_setup(
            register(f"REP{index}"),
            detected_at=PERIOD_START + timedelta(days=index * 10),
        )

    report = evaluate(connection, completed_run.id, as_of=AS_OF, persist=False)

    assert report.overall.sample_size == 6
    assert report.monotonicity.monotonic is None
    assert report.walk_forward.stable is None
    payload = report.metrics_payload()
    assert payload["score_monotonicity"]["undetermined_reason"]
    assert payload["walk_forward"]["caveat"]
    assert "No headline metrics" in report.summary()


def test_a_population_argus_never_committed_to_has_no_expectancy(
    connection: Connection, register, seed_setup, completed_run
):
    """Expectancy is what you get for acting on ARGUS's signals.

    Ten concluded setups, none of them qualified, means ARGUS never acted
    — so precision, hit rate and expectancy have no denominator and come
    back None. The confusion matrix still counts all ten, because "we
    declined and it would have worked" is exactly the false negative
    recall is built from.
    """
    for index in range(6):
        seed_setup(
            register(f"WIN{index}"),
            detected_at=PERIOD_START + timedelta(days=index * 5),
            outcome_status=OutcomeStatus.SUCCESS,
            relative_return=0.25,
        )
    for index in range(4):
        seed_setup(
            register(f"LOSS{index}"),
            detected_at=PERIOD_START + timedelta(days=40 + index * 5),
            outcome_status=OutcomeStatus.FAILED,
            relative_return=-0.10,
            regime=MarketState.DOWN_TREND,
        )

    report = _report(connection, completed_run, config=_low_floor())

    assert report.overall.sample_size == 10
    assert report.overall.expectancy is None
    assert report.overall.precision is None
    assert report.overall.confusion.true_negative == 4
    assert report.overall.confusion.false_negative == 6
    assert report.overall.recall == pytest.approx(0.0)
    assert report.regime.groups["UPTREND"].sample_size == 6
    assert report.regime.groups["DOWN_TREND"].sample_size == 4


def test_a_qualified_population_produces_real_headline_numbers(
    connection: Connection, register, seed_setup, lineage: Lineage, completed_run
):
    """The same path with setups ARGUS actually committed to.

    Each setup gets a real `signals` row via migration 0007's join key,
    so the numbers here travel the same route a production report's would.
    """

    def _qualified(ticker: str, at):
        security_id = register(ticker)
        signal = score_candidate(
            scoring_inputs(security_id, cross=adequate()), as_of=at, lineage=lineage
        )
        return security_id, write_signal(connection, signal)

    for index in range(6):
        detected = PERIOD_START + timedelta(days=index * 5)
        security_id, signal_id = _qualified(f"QWIN{index}", detected)
        seed_setup(
            security_id,
            detected_at=detected,
            outcome_status=OutcomeStatus.SUCCESS,
            relative_return=0.25,
            qualifying_signal_id=signal_id,
        )
    for index in range(4):
        detected = PERIOD_START + timedelta(days=40 + index * 5)
        security_id, signal_id = _qualified(f"QLOSS{index}", detected)
        seed_setup(
            security_id,
            detected_at=detected,
            outcome_status=OutcomeStatus.FAILED,
            relative_return=-0.10,
            regime=MarketState.DOWN_TREND,
            qualifying_signal_id=signal_id,
        )

    report = _report(connection, completed_run, config=_low_floor())

    assert report.overall.sample_size == 10
    assert report.overall.precision == pytest.approx(0.6)
    assert report.overall.recall == pytest.approx(1.0)
    assert report.overall.hit_rate == pytest.approx(0.6)
    assert report.overall.expectancy == pytest.approx(0.11)
    assert report.overall.max_drawdown == pytest.approx(-0.40)
    # Every one of them scored in the same band, so exactly one bucket is
    # reportable and monotonicity has nothing to compare it against.
    assert report.monotonicity.monotonic is None
    assert report.monotonicity.reportable_buckets == 1


def test_the_stored_report_says_which_dataset_and_configuration_produced_it(
    connection: Connection, register, seed_setup, completed_run
):
    """A report that did not say which labelling it evaluated would become
    un-interpretable the first time the success criterion is revised —
    which migration 0007 exists to make possible, so it will happen."""
    seed_setup(register("PROV"))
    report = evaluate(connection, completed_run.id, as_of=AS_OF)

    payload = report.metrics_payload()
    assert payload["dataset"]["as_of"] == AS_OF.isoformat()
    assert payload["config_version"].startswith("argus-evaluation-")
    assert payload["overall"]["return_basis"] == "benchmark_relative"


def test_the_headline_columns_survive_the_check_constraints(
    connection: Connection, register, seed_setup, completed_run
):
    """Module 03 constrains precision/recall/hit_rate to 0..1.

    A metric that ever went outside that range would be rejected by the
    database rather than stored — which is worth knowing works, because
    the constraint is the last line of defence against a rate computed
    with the wrong denominator.
    """
    for index in range(4):
        seed_setup(
            register(f"BOUND{index}"),
            detected_at=PERIOD_START + timedelta(days=index),
        )

    report = evaluate(connection, completed_run.id, as_of=AS_OF)

    assert report.report_id is not None
    for value in (report.overall.precision, report.overall.recall, report.overall.hit_rate):
        assert value is None or 0.0 <= value <= 1.0
