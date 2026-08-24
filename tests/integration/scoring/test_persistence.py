"""Storing signals: the lineage, the CHECK constraint, and immutability."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from core.risk_context.invalidation import EligibilityTrend
from core.scoring.components import COMPONENT_NAMES
from core.scoring.config import (
    PROBABILITY_DEFINITION,
    ScoringConfig,
    publish_scoring_configuration,
)
from core.scoring.engine import ScoringDecision, score_candidate, score_candidates
from core.scoring.persistence import SignalNotPersistable, write_signal, write_signals
from infra.db.enums import EvidenceStatus
from infra.db.schema.intelligence import signals
from tests.integration.scoring.conftest import AS_OF
from tests.unit.scoring.factories import adequate, insufficient, scoring_inputs

#: `signals` stores scores as NUMERIC(6, 3), so a round-trip is exact only
#: to the column's precision.
STORED_PRECISION = 0.001


def _raw_row(signal, lineage) -> dict:
    """The identity columns only, for an insert that bypasses the writer."""
    return {
        "security_id": signal.security_id,
        "event_time": signal.event_time,
        "evidence_status": EvidenceStatus.INSUFFICIENT_EVIDENCE.value,
        "data_snapshot_id": lineage.data_snapshot_id,
        "scoring_configuration_id": lineage.scoring_configuration_id,
        "target_model_version_id": lineage.target_model_version_id,
        "feature_schema_version_id": lineage.feature_schema_version_id,
        "universe_version_id": lineage.universe_version_id,
        "detection_configuration_id": lineage.detection_configuration_id,
    }


def _scored(security_id, lineage, **kwargs):
    return score_candidate(
        scoring_inputs(security_id, cross=adequate(), **kwargs),
        as_of=AS_OF,
        lineage=lineage,
    )


# --------------------------------------------------------------------------
# The configuration row
# --------------------------------------------------------------------------


def test_publishing_the_configuration_is_idempotent_by_checksum(connection):
    """Two runs under the same weights cite the same row; a recalibration
    becomes a new one, because the table is append-only."""
    first = publish_scoring_configuration(connection, ScoringConfig())
    second = publish_scoring_configuration(connection, ScoringConfig())

    assert first == second


def test_the_stored_definition_can_rebuild_the_configuration(connection, scoring_configuration_id):
    """The point of versioning the weights: a signal scored under them
    stays re-derivable after they have been replaced twice."""
    from infra.db.schema.versioning import scoring_configuration

    row = connection.execute(
        select(scoring_configuration).where(scoring_configuration.c.id == scoring_configuration_id)
    ).one()
    rebuilt = ScoringConfig.from_definition(row.definition)

    assert rebuilt.content_checksum() == ScoringConfig().content_checksum()
    assert row.definition["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def test_a_scored_signal_round_trips_with_its_full_lineage(connection, register, lineage):
    security_id = register("SCORED")
    signal = _scored(security_id, lineage)
    assert signal.decision is ScoringDecision.SCORED

    signal_id = write_signal(connection, signal)
    row = connection.execute(select(signals).where(signals.c.id == signal_id)).one()

    assert row.evidence_status == EvidenceStatus.SCORED.value
    # NUMERIC(6, 3): the stored value is the computed one to the column's
    # own precision, which is what reproducibility means for a stored row.
    assert float(row.argus_score) == pytest.approx(signal.argus_score, abs=STORED_PRECISION)
    assert float(row.confidence) == pytest.approx(signal.confidence, abs=STORED_PRECISION)
    assert float(row.opportunity_score) == pytest.approx(
        signal.opportunity_score, abs=STORED_PRECISION
    )
    assert float(row.risk_score) == pytest.approx(signal.risk_score, abs=STORED_PRECISION)
    for name, value in (
        ("target_model_version_id", lineage.target_model_version_id),
        ("feature_schema_version_id", lineage.feature_schema_version_id),
        ("data_snapshot_id", lineage.data_snapshot_id),
        ("scoring_configuration_id", lineage.scoring_configuration_id),
        ("universe_version_id", lineage.universe_version_id),
        ("detection_configuration_id", lineage.detection_configuration_id),
    ):
        assert getattr(row, name) == value


def test_every_component_reaches_its_own_column(connection, register, lineage):
    """The explainability guarantee, checked against the stored row.

    The seven components and the seven `component_*` columns correspond
    exactly, so a reader of the row alone can see what produced the score.
    """
    signal = _scored(register("PARTS"), lineage)
    signal_id = write_signal(connection, signal)
    row = connection.execute(select(signals).where(signals.c.id == signal_id)).one()

    for name in COMPONENT_NAMES:
        stored = getattr(row, f"component_{name}")
        expected = signal.component_value(name)
        if expected is None:
            assert stored is None, name
        else:
            assert float(stored) == pytest.approx(expected, abs=STORED_PRECISION), name


def test_the_stored_breakdown_reconstructs_the_composite(connection, register, lineage):
    """A score whose parts do not add back up would make the breakdown
    decorative. Renormalized over the components that measured, exactly as
    the engine does."""
    signal = _scored(register("SUMS"), lineage)
    weights = ScoringConfig().weights.as_dict()

    measured = [
        (weights[name], signal.component_value(name))
        for name in COMPONENT_NAMES
        if weights[name] > 0 and signal.component_value(name) is not None
    ]
    total = sum(weight for weight, _ in measured)
    rebuilt = sum(weight * value for weight, value in measured) / total

    assert rebuilt == pytest.approx(signal.argus_score)


def test_an_insufficient_evidence_row_carries_no_numbers_at_all(connection, register, lineage):
    """Module 03's CHECK constraint is what makes "deliberately not
    scored" structurally different from "scored low". This asserts the
    module produces rows the constraint accepts, in the NULL shape."""
    signal = score_candidate(
        scoring_inputs(register("THIN"), cross=insufficient()),
        as_of=AS_OF,
        lineage=lineage,
    )
    assert signal.decision is ScoringDecision.INSUFFICIENT_EVIDENCE

    signal_id = write_signal(connection, signal)
    row = connection.execute(select(signals).where(signals.c.id == signal_id)).one()

    assert row.evidence_status == EvidenceStatus.INSUFFICIENT_EVIDENCE.value
    assert row.argus_score is None
    assert row.confidence is None
    assert row.opportunity_score is None
    assert row.risk_score is None
    assert row.probability is None


def test_the_database_would_reject_a_half_scored_row(connection, register, lineage):
    """Proof the constraint above is load-bearing rather than merely
    satisfied. A SCORED row missing one of its four numbers is refused by
    PostgreSQL, not by this module's politeness."""
    from psycopg import errors
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError) as raised:
        connection.execute(
            signals.insert().values(
                security_id=register("HALF"),
                event_time=AS_OF,
                evidence_status=EvidenceStatus.SCORED.value,
                argus_score=80.0,
                confidence=None,
                opportunity_score=50.0,
                risk_score=20.0,
                **{
                    name: getattr(lineage, name)
                    for name in (
                        "target_model_version_id",
                        "feature_schema_version_id",
                        "data_snapshot_id",
                        "scoring_configuration_id",
                        "universe_version_id",
                        "detection_configuration_id",
                    )
                },
            )
        )
    assert isinstance(raised.value.orig, errors.CheckViolation)


def test_probability_is_stored_null_with_its_definition_and_status(connection, register, lineage):
    """The structure is present and the absence is unambiguous. The status
    rides along in the definition text because `signals` has no JSONB
    column to carry it — see the module README."""
    signal_id = write_signal(connection, _scored(register("PROB"), lineage))
    row = connection.execute(select(signals).where(signals.c.id == signal_id)).one()

    assert row.probability is None
    assert PROBABILITY_DEFINITION in row.probability_definition
    assert "NOT_YET_CALIBRATED" in row.probability_definition


# --------------------------------------------------------------------------
# Immutability and re-runs
# --------------------------------------------------------------------------


def test_rescoring_the_same_candidate_produces_an_identical_result(connection, register, lineage):
    """Reproducibility, as the brief defines it: the same configuration
    IDs against the same snapshot must produce the same numbers."""
    security_id = register("REPRO")
    inputs = scoring_inputs(security_id, cross=adequate())

    first = score_candidate(inputs, as_of=AS_OF, lineage=lineage)
    second = score_candidate(inputs, as_of=AS_OF, lineage=lineage)

    assert first.as_dict() == second.as_dict()


def test_a_repeated_write_does_not_create_a_second_row(connection, register, lineage):
    """`signals` has no unique constraint, so this guard is the only thing
    stopping a re-run from leaving two immutable rows with no way to tell
    which one a decision cited. See the README on why a migration is the
    real fix."""
    signal = _scored(register("ONCE"), lineage)

    assert write_signal(connection, signal) is not None
    assert write_signal(connection, signal) is None

    count = connection.execute(
        select(signals).where(signals.c.security_id == signal.security_id)
    ).all()
    assert len(count) == 1


def test_the_database_itself_rejects_a_duplicate_scoring(connection, register, lineage):
    """Proof the protection is the index, not the writer's politeness.

    Bypasses `write_signal` entirely and inserts the same identity twice
    through raw SQL. Before migration 0005 this succeeded silently.
    """
    from psycopg import errors
    from sqlalchemy.exc import IntegrityError

    signal = _scored(register("DUPE"), lineage)
    write_signal(connection, signal)

    savepoint = connection.begin_nested()
    with pytest.raises(IntegrityError) as raised:
        connection.execute(signals.insert().values(_raw_row(signal, lineage)))
    assert isinstance(raised.value.orig, errors.UniqueViolation)
    savepoint.rollback()


def test_the_uniqueness_index_does_not_block_corrections(connection, register, lineage):
    """The index is partial for exactly this reason.

    A correction carries the same identity tuple by design — it is the
    same computation, rescored — and points at the row it replaces. An
    unconditional constraint would have made corrections impossible, so
    the index covers only rows with no `supersedes_signal_id`.
    """
    signal = _scored(register("CORRIGE"), lineage)
    original = write_signal(connection, signal)

    first = write_signal(connection, signal, supersedes=original)
    second = write_signal(connection, signal, supersedes=original)

    assert first is not None
    assert second is not None, "a second correction must also be permitted"
    assert len({original, first, second}) == 3


def test_the_detail_column_carries_the_layer_beneath_the_component_numbers(
    connection, register, lineage
):
    """Module 03's comment promises a user can always see why a score is
    what it is. The seven columns give the component values; this is where
    the readings, the ramps, the unmeasured reasons and the calibration
    status live."""
    signal = _scored(register("DETAIL"), lineage)
    signal_id = write_signal(connection, signal)
    row = connection.execute(select(signals).where(signals.c.id == signal_id)).one()

    detail = row.detail
    assert detail["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"
    assert "NOT_YET_CALIBRATED" in detail["probability_status"]
    assert detail["weight_coverage"] == pytest.approx(signal.weight_coverage)
    assert set(detail["components"]) == set(COMPONENT_NAMES)
    # The raw reading and what the ramp made of it, both recoverable.
    historical = detail["components"]["volatility_structure"]
    assert historical["inputs"]["atr_percentile"] is not None
    assert historical["normalized"]["atr_percentile"] is not None
    assert detail["confidence_assessment"]["factors"]["sample_sufficiency"]["value"] is not None


def test_a_refusal_records_why_in_the_detail_column(connection, register, lineage):
    """An INSUFFICIENT_EVIDENCE row has five NULL numbers. Without the
    detail column the reason lived nowhere at all."""
    signal = score_candidate(
        scoring_inputs(register("WHYNOT"), cross=insufficient()),
        as_of=AS_OF,
        lineage=lineage,
    )
    signal_id = write_signal(connection, signal)
    row = connection.execute(select(signals).where(signals.c.id == signal_id)).one()

    assert row.detail["decision"] == "INSUFFICIENT_EVIDENCE"
    assert "weight" in row.detail["verdict"]["reason"]
    assert row.detail["verdict"]["detail"]["unmeasured_components"] == ["historical_evidence"]


def test_a_correction_is_a_new_row_pointing_at_the_original(connection, register, lineage):
    """The original stays exactly as written; the append-only trigger
    would refuse anything else."""
    signal = _scored(register("FIXED"), lineage)
    original = write_signal(connection, signal)

    corrected = write_signal(connection, signal, supersedes=original)
    row = connection.execute(select(signals).where(signals.c.id == corrected)).one()

    assert corrected != original
    assert row.supersedes_signal_id == original


def test_a_stored_signal_cannot_be_edited_or_deleted(connection, register, lineage):
    from psycopg import errors
    from sqlalchemy.exc import IntegrityError

    signal_id = write_signal(connection, _scored(register("FROZEN"), lineage))

    for statement in (
        signals.update().where(signals.c.id == signal_id).values(argus_score=1.0),
        signals.delete().where(signals.c.id == signal_id),
    ):
        # A savepoint per attempt: the failed statement has to be rolled
        # back without discarding the row the next attempt targets.
        savepoint = connection.begin_nested()
        with pytest.raises(IntegrityError) as raised:
            connection.execute(statement)
        assert isinstance(raised.value.orig, errors.RestrictViolation)
        savepoint.rollback()

    assert connection.execute(select(signals).where(signals.c.id == signal_id)).one()


def test_gated_candidates_write_nothing(connection, register, lineage):
    """An INSUFFICIENT_EVIDENCE row for a broken thesis would say ARGUS
    could not gather evidence, which is false: it gathered evidence and
    the evidence says the setup is over."""
    signal = _scored(
        register("GATED"), lineage, eligibility_trend=EligibilityTrend.LOST_ELIGIBILITY
    )
    assert signal.decision is ScoringDecision.GATED_LOST_ELIGIBILITY

    with pytest.raises(SignalNotPersistable):
        write_signal(connection, signal)

    assert (
        connection.execute(select(signals).where(signals.c.security_id == signal.security_id)).all()
        == []
    )


def test_a_batch_write_skips_the_gated_ones_and_reports_what_it_wrote(
    connection, register, lineage
):
    candidates = [
        scoring_inputs(register("BATCHA"), cross=adequate()),
        scoring_inputs(register("BATCHB"), cross=insufficient()),
        scoring_inputs(
            register("BATCHC"),
            cross=adequate(),
            eligibility_trend=EligibilityTrend.LOST_ELIGIBILITY,
        ),
    ]
    results = score_candidates(candidates, as_of=AS_OF, lineage=lineage)

    written = write_signals(connection, results)

    assert len(results) == 3
    assert len(written) == 2
