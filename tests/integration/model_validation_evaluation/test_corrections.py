"""The three Part 0 corrections, each with the evidence that it took.

Migration 0007 changed the shape of two tables that everything downstream
reads. These tests are the record that the change did what it was for —
and, in two cases, that the *old* behaviour is genuinely gone rather than
merely no longer exercised.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from core.historical_similarity.cases import load_cases
from core.lifecycle.config import LifecycleConfig
from core.lifecycle.engine import CandidateObservation, advance_lifecycle, open_setup
from core.outcome_tracking.engine import record_outcome
from core.scoring.engine import Lineage
from core.scoring.persistence import resolve_signal_id, write_signal
from infra.db.enums import MarketState, OutcomeStatus, SetupLifecycleStatus
from infra.db.schema.setups import setup_outcomes, setups
from infra.db.schema.versioning import feature_vectors
from tests.integration.model_validation_evaluation.conftest import AS_OF, PERIOD_START

# --------------------------------------------------------------------------
# Correction 1: setup_outcomes supports recomputation
# --------------------------------------------------------------------------


def test_an_outcome_can_be_recomputed_under_a_new_snapshot(
    connection: Connection,
    register,
    seed_setup,
    lineage: Lineage,
    second_snapshot: UUID,
):
    """The behaviour Module 15 could not have: a second labelling.

    Before 0007, `setup_outcomes` was unique on `setup_id` alone and this
    insert wrote nothing — so revising the success criterion (which Module
    15 explicitly called an invented placeholder) could never relabel the
    dataset.
    """
    security_id = register("MLSS")
    setup_id = seed_setup(security_id, outcome_status=OutcomeStatus.FAILED)

    connection.execute(
        setup_outcomes.insert().values(
            setup_id=setup_id,
            outcome_status=OutcomeStatus.SUCCESS.value,
            benchmark_relative_return=0.22,
            data_snapshot_id=second_snapshot,
            recorded_at=AS_OF,
        )
    )

    rows = connection.execute(
        select(setup_outcomes.c.outcome_status, setup_outcomes.c.data_snapshot_id)
        .where(setup_outcomes.c.setup_id == setup_id)
        .order_by(setup_outcomes.c.recorded_at)
    ).all()

    assert len(rows) == 2
    # Both rows survive. The first is still the right answer to its own
    # question, and 0007 chose uniqueness over supersession precisely so
    # neither has to be marked as the loser.
    assert {row.outcome_status for row in rows} == {"FAILED", "SUCCESS"}
    assert {row.data_snapshot_id for row in rows} == {
        lineage.data_snapshot_id,
        second_snapshot,
    }


def test_the_same_snapshot_still_cannot_write_two_outcomes_for_one_setup(
    connection: Connection, register, seed_setup, lineage: Lineage
):
    """Recomputation is permitted; quiet duplication is not.

    If this constraint were merely dropped rather than replaced, the same
    run could write the same outcome twice and every count downstream
    would double.
    """
    security_id = register("SLS")
    setup_id = seed_setup(security_id)

    with pytest.raises(IntegrityError) as exc_info:
        connection.execute(
            setup_outcomes.insert().values(
                setup_id=setup_id,
                outcome_status=OutcomeStatus.FAILED.value,
                data_snapshot_id=lineage.data_snapshot_id,
            )
        )
    assert "uq_setup_outcomes_setup_snapshot" in str(exc_info.value)


def test_record_outcome_writes_again_under_a_different_snapshot(
    connection: Connection, register, seed_setup, second_snapshot: UUID
):
    """Module 15's own writer, through its own conflict target.

    The insert-only guard is still there — it just keys on the pair now.
    """
    from core.outcome_tracking.case_record import CaseRecord, StageChecklist
    from core.outcome_tracking.classification import OutcomeClassification
    from core.outcome_tracking.excursion import Excursion

    security_id = register("HIVE")
    setup_id = seed_setup(security_id)

    case = CaseRecord(
        setup_id=setup_id,
        security_id=security_id,
        stages=StageChecklist(
            detected_at=PERIOD_START,
            qualified_at=None,
            activated_at=None,
            concluded_at=PERIOD_START + timedelta(days=60),
            terminal_event_type="expired_unqualified",
            stages_reached=("DETECTION",),
            retreat_count=0,
            retreats=(),
            events_recorded=1,
        ),
        excursion=Excursion(),
        classification=OutcomeClassification(
            status=OutcomeStatus.FAILED, reason="module17 correction-1 evidence"
        ),
    )

    first = record_outcome(connection, case, data_snapshot_id=second_snapshot)
    second = record_outcome(connection, case, data_snapshot_id=second_snapshot)

    assert first is not None
    assert second is None, "a re-run under the same snapshot must write nothing"


def test_the_case_loader_picks_the_most_recent_labelling_deterministically(
    connection: Connection, register, seed_setup, lineage: Lineage, second_snapshot: UUID
):
    """The ripple 0007 created, and the fix.

    Module 11's `DISTINCT ON (s.id)` ordered only the feature vector,
    because the outcome could not be ambiguous. Two outcome rows per setup
    made it ambiguous, so the ordering now names the outcome first. Without
    that, which label a similarity lookup saw would be whatever the planner
    happened to return.
    """
    security_id = register("ALX")
    setup_id = seed_setup(
        security_id,
        outcome_status=OutcomeStatus.FAILED,
        relative_return=-0.20,
    )
    connection.execute(
        feature_vectors.insert().values(
            security_id=security_id,
            feature_schema_version_id=lineage.feature_schema_version_id,
            event_time=PERIOD_START - timedelta(days=1),
            availability_time=PERIOD_START - timedelta(days=1),
            features={"peak_to_trough_decline": -0.6, "base_length_days": 120.0},
        )
    )
    # A later labelling under a new snapshot says SUCCESS.
    connection.execute(
        setup_outcomes.insert().values(
            setup_id=setup_id,
            outcome_status=OutcomeStatus.SUCCESS.value,
            benchmark_relative_return=0.25,
            data_snapshot_id=second_snapshot,
            recorded_at=AS_OF,
        )
    )

    cases = load_cases(connection, AS_OF + timedelta(days=1), lineage.feature_schema_version_id)

    assert len(cases) == 1
    assert cases.outcomes.loc[setup_id, "outcome_status"] == "SUCCESS"


# --------------------------------------------------------------------------
# Correction 2: setups record their schema version and qualifying signal
# --------------------------------------------------------------------------


def test_a_setup_records_its_own_feature_schema_version(
    connection: Connection, register, lineage: Lineage
):
    """Not the caller's memory of it. For a replay spanning several schema
    versions, "the caller remembered correctly" is not a record."""
    security_id = register("QBTS")
    setup_id, _ = open_setup(connection, security_id, as_of=PERIOD_START, lineage=lineage)

    stored = connection.execute(
        select(setups.c.feature_schema_version_id).where(setups.c.id == setup_id)
    ).scalar_one()

    assert stored == lineage.feature_schema_version_id


def test_a_setup_cannot_be_written_without_a_feature_schema_version(
    connection: Connection, register, lineage: Lineage
):
    """NOT NULL, like the other five lineage columns. A setup that does not
    say which schema described it is not reproducible."""
    security_id = register("NOFSV")

    with pytest.raises(IntegrityError):
        connection.execute(
            setups.insert().values(
                security_id=security_id,
                detected_at=PERIOD_START,
                target_model_version_id=lineage.target_model_version_id,
                detection_configuration_id=lineage.detection_configuration_id,
                universe_version_id=lineage.universe_version_id,
            )
        )


def test_qualification_records_the_signal_that_qualified_the_setup(
    connection: Connection, register, lineage: Lineage
):
    """Migration 0007's join key, populated by Module 14.

    Module 15 worked around its absence by reading the QUALIFIED event's
    JSONB payload. That copy is still written — the event log has to stand
    alone — but correlating score against outcome at scale needs a join.
    """
    from core.scoring.engine import score_candidate
    from tests.unit.scoring.factories import adequate, scoring_inputs

    security_id = register("QUALME")
    setup_id, _ = open_setup(connection, security_id, as_of=PERIOD_START, lineage=lineage)

    signal = score_candidate(
        scoring_inputs(security_id, cross=adequate()),
        as_of=PERIOD_START + timedelta(days=1),
        lineage=lineage,
    )
    signal_id = write_signal(connection, signal)
    assert signal_id is not None

    report = advance_lifecycle(
        connection,
        [
            CandidateObservation(
                security_id=security_id,
                state=MarketState.CONSOLIDATION,
                signal=signal,
                signal_id=signal_id,
            )
        ],
        as_of=PERIOD_START + timedelta(days=1),
        lineage=lineage,
        config=LifecycleConfig(),
    )

    assert report.results[0].status is SetupLifecycleStatus.QUALIFICATION
    stored = connection.execute(
        select(setups.c.qualifying_signal_id).where(setups.c.id == setup_id)
    ).scalar_one()
    assert stored == signal_id


def test_a_setup_that_never_qualified_carries_a_null_join_key(
    connection: Connection, register, lineage: Lineage
):
    """NULL is a fact — "detected, not committed to" — not missing data.

    It is also the normal case: Module 14's bootstrap analysis means
    essentially every setup sits at DETECTION today.
    """
    security_id = register("NOQUAL")
    setup_id, _ = open_setup(connection, security_id, as_of=PERIOD_START, lineage=lineage)

    stored = connection.execute(
        select(setups.c.qualifying_signal_id).where(setups.c.id == setup_id)
    ).scalar_one()

    assert stored is None


def test_the_join_key_is_still_populated_when_the_signal_was_already_written(
    connection: Connection, register, lineage: Lineage
):
    """`write_signal` returns None for a row that already existed.

    A caller that treated that as "no ID" would silently stop populating
    the join key on any re-run over the same date — which is exactly what
    a replay does.
    """
    from core.scoring.engine import score_candidate
    from tests.unit.scoring.factories import adequate, scoring_inputs

    security_id = register("REWRITE")
    signal = score_candidate(
        scoring_inputs(security_id, cross=adequate()),
        as_of=PERIOD_START,
        lineage=lineage,
    )
    first = write_signal(connection, signal)
    again = write_signal(connection, signal)

    assert first is not None
    assert again is None
    assert resolve_signal_id(connection, signal) == first


def test_the_qualifying_signal_must_be_a_real_signal_row(
    connection: Connection, register, lineage: Lineage
):
    """A foreign key, not a loose UUID. A payload copy could hold anything;
    this cannot."""
    security_id = register("FKCHECK")

    with pytest.raises(IntegrityError):
        connection.execute(
            setups.insert().values(
                security_id=security_id,
                detected_at=PERIOD_START,
                target_model_version_id=lineage.target_model_version_id,
                detection_configuration_id=lineage.detection_configuration_id,
                universe_version_id=lineage.universe_version_id,
                feature_schema_version_id=lineage.feature_schema_version_id,
                qualifying_signal_id=uuid4(),
            )
        )


def test_migration_0007_left_setup_id_indexed_for_lookups(connection: Connection):
    """The dropped unique constraint took its implicit index with it.

    The replacement constraint's index leads with `setup_id`, so lookups
    by setup alone are still served — asserted rather than assumed,
    because "we lost the index on the busiest join column" is the kind of
    regression that only shows up at scale.
    """
    indexes = (
        connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = 'setup_outcomes'")
        )
        .scalars()
        .all()
    )

    leading = [
        definition
        for definition in indexes
        if "(setup_id" in definition.replace(" ", "").replace("USINGbtree", "")
    ]
    assert leading, f"nothing indexes setup_outcomes by setup_id first: {indexes}"
