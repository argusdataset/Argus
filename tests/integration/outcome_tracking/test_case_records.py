"""Complete case records — for failures exactly as much as for successes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from core.outcome_tracking.case_record import (
    AWAKENING_METRICS,
    CONSOLIDATION_METRICS,
    CONTEXT_METRICS,
    DECLINE_METRICS,
)
from core.outcome_tracking.engine import (
    SetupNotConcluded,
    compute_case,
    process_concluded_setups,
    record_outcome,
)
from infra.db.enums import FalsePositiveType, OutcomeStatus, ReviewConfidence
from infra.db.schema.setups import setup_outcomes
from tests.integration.outcome_tracking.conftest import AS_OF, FIRST_BAR, write_bars

DETECTED = datetime(2024, 1, 16, 21, 0, tzinfo=UTC)
ACTIVATED = datetime(2024, 1, 30, 21, 0, tzinfo=UTC)
TERMINAL = datetime(2024, 3, 26, 21, 0, tzinfo=UTC)

#: Rises past +10% well inside the horizon.
WINNER = [100.0 * (1.004**step) for step in range(120)]
#: Falls past -5% shortly after activation.
LOSER = [100.0 * (0.997**step) for step in range(120)]
#: Drifts sideways: neither threshold is ever reached.
DRIFTER = [100.0 + (step % 3) * 0.1 for step in range(120)]


@pytest.fixture
def case(connection, register, concluded_setup, snapshot_id):
    """Build one concluded setup over a chosen series and compute its case."""

    def _case(ticker: str, closes: list[float], **kwargs):
        security_id = register(ticker)
        write_bars(connection, security_id, closes, start=FIRST_BAR)
        setup_id = concluded_setup(
            security_id,
            detected_at=kwargs.pop("detected_at", DETECTED),
            activated_at=kwargs.pop("activated_at", ACTIVATED),
            terminal_at=kwargs.pop("terminal_at", TERMINAL),
            **kwargs,
        )
        return compute_case(connection, setup_id, as_of=AS_OF, data_snapshot_id=snapshot_id)

    return _case


# --------------------------------------------------------------------------
# Failures are exactly as complete as successes
# --------------------------------------------------------------------------


def test_a_success_and_a_failure_produce_identically_complete_records(case):
    """The guarantee the brief singles out, proven rather than inspected.

    A dataset with richer information about wins than losses teaches
    Module 17 that wins are more knowable, which is exactly backwards. The
    assembly has no branch on `OutcomeStatus` anywhere, so this holds by
    construction — and this is what would notice if a branch appeared.
    """
    won = case("WINNER", WINNER)
    lost = case("LOSER", LOSER)

    assert won.classification.status is OutcomeStatus.SUCCESS
    assert lost.classification.status is OutcomeStatus.FAILED

    won_payload, lost_payload = won.as_dict(), lost.as_dict()
    assert set(won_payload) == set(lost_payload)
    assert set(won_payload["outcome"]) == set(lost_payload["outcome"])
    assert set(won_payload["stages"]) == set(lost_payload["stages"])

    for payload in (won_payload, lost_payload):
        for block in ("decline_metrics", "consolidation_metrics", "awakening_metrics"):
            assert set(payload[block]) == set(won_payload[block])

    # And every numeric field the schema has room for is populated in both.
    for record in (won, lost):
        excursion = record.excursion
        assert excursion.mfe is not None
        assert excursion.mae is not None
        assert excursion.realized_return is not None
        assert excursion.volatility_adjusted_outcome is not None
        assert excursion.window is not None


def test_the_stored_rows_are_equally_complete(connection, case, snapshot_id):
    """The same guarantee, at the row level. A `FAILED` row with fewer
    populated columns than a `SUCCESS` one would be invisible in the
    object and obvious only once the dataset was already built."""
    won, lost = case("WON2", WINNER), case("LOST2", LOSER)
    for record in (won, lost):
        record_outcome(connection, record, data_snapshot_id=snapshot_id)

    rows = {
        row.setup_id: row
        for row in connection.execute(
            select(setup_outcomes).where(
                setup_outcomes.c.setup_id.in_([won.setup_id, lost.setup_id])
            )
        )
    }
    populated = {
        setup_id: {key for key, value in row._mapping.items() if value is not None}
        for setup_id, row in rows.items()
    }

    # `false_positive_type` is legitimately null on a success and set on a
    # failure — that asymmetry is the taxonomy working, not a gap.
    ignore = {"false_positive_type"}
    assert populated[won.setup_id] - ignore == populated[lost.setup_id] - ignore


def test_a_failure_carries_a_false_positive_type_and_a_success_does_not(case):
    assert case("FPWIN", WINNER).classification.false_positive_type is None
    assert case("FPLOSE", LOSER).classification.false_positive_type is not None


# --------------------------------------------------------------------------
# NO_VALID_OUTCOME
# --------------------------------------------------------------------------


def test_a_setup_that_never_activated_gets_a_real_row_explaining_why(connection, case, snapshot_id):
    """Not a null record and not an absent one. "We could not measure
    this" is a fact about the dataset, and only a row can carry it."""
    record = case(
        "NEVERACTIVE",
        DRIFTER,
        activated_at=None,
        terminal_event="expired_unqualified",
        market_state="CONSOLIDATION",
    )

    assert record.classification.status is OutcomeStatus.NO_VALID_OUTCOME
    assert record.classification.false_positive_type is FalsePositiveType.A_NO_PATTERN
    assert "never activated" in record.classification.reason
    assert record.stages.activated_at is None
    assert record.stages.detected_at == DETECTED

    outcome_id = record_outcome(connection, record, data_snapshot_id=snapshot_id)
    row = connection.execute(select(setup_outcomes).where(setup_outcomes.c.id == outcome_id)).one()

    assert row.outcome_status == OutcomeStatus.NO_VALID_OUTCOME.value
    assert row.mfe is None and row.realized_return is None
    assert row.false_positive_type == FalsePositiveType.A_NO_PATTERN.value
    assert row.review_confidence == ReviewConfidence.LOW.value
    assert row.data_snapshot_id == snapshot_id


# --------------------------------------------------------------------------
# Terminal event mapping, against real event histories
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("terminal_event", "closes", "expected"),
    [
        ("endpoint_reached", WINNER, OutcomeStatus.SUCCESS),
        ("endpoint_reached", LOSER, OutcomeStatus.FAILED),
        ("endpoint_reached", DRIFTER, OutcomeStatus.EXPIRED),
        ("expired_active", DRIFTER, OutcomeStatus.EXPIRED),
        ("invalidated_lost_eligibility", DRIFTER, OutcomeStatus.INVALIDATED),
        ("invalidated_ineligible", DRIFTER, OutcomeStatus.INVALIDATED),
    ],
)
def test_each_terminal_event_maps_to_the_right_status(case, terminal_event, closes, expected):
    """Driven through Module 14's real event histories, not hand-built
    payloads — the coupling between the two modules is itself something
    that can break."""
    record = case(
        f"MAP{terminal_event[:6]}{expected.value[:4]}",
        closes,
        terminal_event=terminal_event,
    )

    assert record.classification.status is expected


def test_an_unconcluded_setup_has_no_outcome_to_compute(connection, register, lineage, snapshot_id):
    from core.lifecycle.engine import open_setup

    setup_id, _ = open_setup(connection, register("STILLOPEN"), as_of=DETECTED, lineage=lineage)

    with pytest.raises(SetupNotConcluded):
        compute_case(connection, setup_id, as_of=AS_OF, data_snapshot_id=snapshot_id)


# --------------------------------------------------------------------------
# The rest of the CASE record
# --------------------------------------------------------------------------


def test_retreat_history_is_carried_into_the_case_record(case):
    """Module 14 records retreats as events inside ACTIVE rather than as
    demotions. A setup that retreated twice before resolving is a
    materially different case, and a record that dropped them would make
    the two indistinguishable."""
    retreats = (
        datetime(2024, 2, 14, 21, 0, tzinfo=UTC),
        datetime(2024, 2, 28, 21, 0, tzinfo=UTC),
    )
    record = case("RETREATED", WINNER, retreats=retreats)

    assert record.stages.retreat_count == 2
    assert record.stages.retreats == retreats
    assert record.as_dict()["stages"]["retreat_count"] == 2


def test_the_stage_checklist_records_which_stages_were_reached(case):
    record = case("STAGES", WINNER)

    assert record.stages.detected_at == DETECTED
    assert record.stages.qualified_at == DETECTED + timedelta(days=1)
    assert record.stages.activated_at == ACTIVATED
    assert record.stages.concluded_at == TERMINAL
    assert record.stages.terminal_event_type == "endpoint_reached"
    assert set(record.stages.stages_reached) == {
        "DETECTION",
        "QUALIFICATION",
        "ACTIVE",
        "OUTCOME",
    }


def test_metric_blocks_have_the_same_keys_whether_or_not_features_existed(case):
    """A missing key and a `None` value read very differently to whatever
    consumes this next, so every named metric is always present."""
    record = case("NOFEATURES", WINNER)

    assert set(record.decline_metrics) == set(DECLINE_METRICS)
    assert set(record.consolidation_metrics) == set(CONSOLIDATION_METRICS)
    assert set(record.awakening_metrics) == set(AWAKENING_METRICS)
    assert set(record.context_metrics) == set(CONTEXT_METRICS)
    assert all(value is None for value in record.decline_metrics.values())


def test_the_record_carries_its_lineage_and_admits_its_calibration_status(case):
    record = case("LINEAGE", WINNER)

    assert set(record.lineage) >= {
        "target_model_version_id",
        "detection_configuration_id",
        "universe_version_id",
        "data_snapshot_id",
        "outcome_configuration",
    }
    assert record.calibration_status == "UNVALIDATED_PLACEHOLDERS"


def test_market_regime_at_outcome_comes_from_the_terminal_event(case):
    record = case("REGIME", WINNER, market_state="UPTREND")

    assert record.market_regime_at_outcome == "UPTREND"


# --------------------------------------------------------------------------
# Batch processing and idempotence
# --------------------------------------------------------------------------


def test_processing_writes_one_row_per_concluded_setup(
    connection, register, concluded_setup, snapshot_id
):
    for ticker, closes in (("BATCHW", WINNER), ("BATCHL", LOSER), ("BATCHD", DRIFTER)):
        security_id = register(ticker)
        write_bars(connection, security_id, closes, start=FIRST_BAR)
        concluded_setup(
            security_id,
            detected_at=DETECTED,
            activated_at=ACTIVATED,
            terminal_at=TERMINAL,
        )

    report = process_concluded_setups(connection, as_of=AS_OF, data_snapshot_id=snapshot_id)

    assert len(report) == 3
    assert len(report.written) == 3
    assert report.by_status() == {
        OutcomeStatus.SUCCESS.value: 1,
        OutcomeStatus.FAILED.value: 1,
        OutcomeStatus.EXPIRED.value: 1,
    }


def test_reprocessing_writes_nothing_new(connection, register, concluded_setup, snapshot_id):
    """A stored outcome cites a snapshot, and a Module 17 run that read it
    must keep reading the same thing."""
    security_id = register("IDEMPOTENT")
    write_bars(connection, security_id, WINNER, start=FIRST_BAR)
    concluded_setup(security_id, detected_at=DETECTED, activated_at=ACTIVATED, terminal_at=TERMINAL)

    first = process_concluded_setups(connection, as_of=AS_OF, data_snapshot_id=snapshot_id)
    second = process_concluded_setups(connection, as_of=AS_OF, data_snapshot_id=snapshot_id)

    assert len(first.written) == 1
    assert second.written == []
    assert len(connection.execute(select(setup_outcomes)).all()) == 1


def test_a_setup_concluded_after_the_query_date_is_not_processed(
    connection, register, concluded_setup, snapshot_id
):
    """Dual mode: replaying an earlier date must not sweep up setups that
    had not finished yet."""
    security_id = register("LATER")
    write_bars(connection, security_id, WINNER, start=FIRST_BAR)
    concluded_setup(security_id, detected_at=DETECTED, activated_at=ACTIVATED, terminal_at=TERMINAL)

    early = process_concluded_setups(
        connection,
        as_of=datetime(2024, 2, 1, 21, 0, tzinfo=UTC),
        data_snapshot_id=snapshot_id,
    )

    assert len(early) == 0
