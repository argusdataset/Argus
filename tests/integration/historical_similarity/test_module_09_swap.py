"""Does this module drop into Module 09's seam without touching its code?

Module 09 built an `AnalogueCounter` Protocol and shipped a placeholder
behind it, explicitly as a stand-in for this module. The claim under test
is narrow and checkable: **substituting this module's real counter
requires no change to Module 09**.

The tests below run Module 09's own eligibility runner with this counter
injected, and assert on Module 09's own gate results — not on anything
this module returns. If the seam were leaky, that would not work.

They also pin the re-derivation of `min_historical_analogues`, including
the uncomfortable part: with an empty case dataset this counter returns 0
for every security, so enabling the gate today would stop the pipeline.
That is arithmetic rather than a defect, and it is asserted here so it
cannot be discovered by surprise in production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection

from core.candidate_detection import (
    DetectionConfig,
    detect_candidates,
    evaluate_eligibility,
    new_run_id,
)
from core.candidate_detection.config import DetectionParameters, EligibilityParameters
from core.candidate_detection.eligibility.analogues import (
    AnalogueCounter,
    PeerProfileAnalogueCounter,
)
from core.feature_engine.vector import BatchFeatureResult, FeatureEvidence, FeatureVector
from core.historical_similarity import (
    RE_DERIVED_MIN_ANALOGUES,
    HistoricalAnalogueCounter,
    SimilarityConfig,
    SimilarityThreshold,
    SimilarityThresholds,
)
from data.canonical_model.records import CanonicalTimeframe
from infra.db.enums import EligibilityGate
from tests.integration.historical_similarity.test_engine import (
    BASE_SHAPE,
    MOMENTUM_SHAPE,
    _features,
)

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)


def _wide_config() -> SimilarityConfig:
    """A radius that admits the fixture's base-shaped cases.

    The shipped radius is an unvalidated placeholder; pinning it here
    keeps these tests about the *swap* rather than about the number.
    """
    default = SimilarityThresholds()
    return SimilarityConfig(
        thresholds=SimilarityThresholds(
            max_distance=SimilarityThreshold(
                value=1_000.0, kind=default.max_distance.kind, rationale="pinned by test"
            )
        )
    )


def _batch(security_ids: list[UUID], shape: dict) -> BatchFeatureResult:
    """A Module 08 batch result for the given securities."""
    payload = {k: v for k, v in _features(shape).items() if k != "_evidence"}
    return BatchFeatureResult(
        as_of=AS_OF,
        feature_schema_version_id=None,
        timeframe=CanonicalTimeframe.DAILY,
        vectors={
            security_id: FeatureVector(
                security_id=security_id,
                as_of=AS_OF,
                feature_schema_version_id=None,
                timeframe=CanonicalTimeframe.DAILY,
                event_time=AS_OF,
                availability_time=AS_OF,
                features=dict(payload),
                evidence=FeatureEvidence(bars_available=800, bars_required=252, missing_inputs={}),
            )
            for security_id in security_ids
        },
        missing_securities={},
    )


# --------------------------------------------------------------------------
# The seam
# --------------------------------------------------------------------------


def test_the_real_counter_satisfies_module_09s_protocol(
    connection: Connection, schema_version_id: UUID
):
    """Structural typing — no import, base class or registration owed."""
    counter = HistoricalAnalogueCounter(connection, schema_version_id)

    assert isinstance(counter, AnalogueCounter)
    assert not isinstance(counter, PeerProfileAnalogueCounter)
    assert PeerProfileAnalogueCounter not in type(counter).__mro__


def test_module_09s_eligibility_runner_accepts_it_unchanged(
    connection: Connection, register, make_case, schema_version_id: UUID, version_ids
):
    """The actual drop-in claim, exercised through Module 09's own code.

    Module 09's `evaluate_eligibility` is called with this counter
    injected, and the assertion is on Module 09's gate result. Nothing in
    `core/candidate_detection/` was modified for this to work.
    """
    from core.candidate_detection.eligibility.gates import EligibilityReport
    from infra.db.schema.identity import universe_membership

    subject = register("SWAPSUB")
    for index in range(20):
        make_case(
            register(f"SWAPCASE{index}"),
            _features(BASE_SHAPE, jitter=0.01 * index, seed=index),
        )
    connection.execute(
        universe_membership.insert().values(
            universe_version_id=version_ids["universe"],
            security_id=subject,
            listing_status="LISTED",
            exchange="NASDAQ",
            listed_from=datetime(2015, 1, 1, tzinfo=UTC),
            listed_to=None,
            interval_evidence="from=price_history;to=price_history",
        )
    )

    features = _batch([subject], BASE_SHAPE)
    pool = detect_candidates(
        features,
        config=DetectionConfig(detection=DetectionParameters(selection_fraction=1.0)),
        run_id=new_run_id(),
    )

    report = evaluate_eligibility(
        connection,
        pool,
        features,
        version_ids["universe"],
        config=DetectionConfig(
            eligibility=EligibilityParameters(min_historical_analogues=RE_DERIVED_MIN_ANALOGUES)
        ),
        analogue_counter=HistoricalAnalogueCounter(
            connection, schema_version_id, config=_wide_config()
        ),
    )

    assert isinstance(report, EligibilityReport)
    gate = report.outcomes[subject].results[EligibilityGate.MINIMUM_HISTORICAL_ANALOGUES]
    assert gate.passed, gate.detail
    assert gate.detail["analogue_count"] >= RE_DERIVED_MIN_ANALOGUES
    assert gate.detail["method"] == "historical_similarity_v1"


def test_the_gate_fails_when_there_are_too_few_analogues(
    connection: Connection, register, make_case, schema_version_id: UUID, version_ids
):
    """The other half — the gate must be capable of rejecting."""
    subject = register("SWAPFEW")
    for index in range(3):
        make_case(
            register(f"FEWCASE{index}"),
            _features(BASE_SHAPE, jitter=0.01 * index, seed=index),
        )

    features = _batch([subject], BASE_SHAPE)
    pool = detect_candidates(
        features,
        config=DetectionConfig(detection=DetectionParameters(selection_fraction=1.0)),
        run_id=new_run_id(),
    )
    report = evaluate_eligibility(
        connection,
        pool,
        features,
        version_ids["universe"],
        config=DetectionConfig(
            eligibility=EligibilityParameters(min_historical_analogues=RE_DERIVED_MIN_ANALOGUES)
        ),
        analogue_counter=HistoricalAnalogueCounter(
            connection, schema_version_id, config=_wide_config()
        ),
    )

    gate = report.outcomes[subject].results[EligibilityGate.MINIMUM_HISTORICAL_ANALOGUES]
    assert not gate.passed
    assert gate.detail["analogue_count"] == 3


# --------------------------------------------------------------------------
# Module 09's contract: zero, never absent
# --------------------------------------------------------------------------


def test_every_requested_security_gets_an_entry(
    connection: Connection, register, schema_version_id: UUID
):
    """Module 09's explicit design: an omission would silently pass the gate."""
    securities = [register(f"ENTRY{i}") for i in range(4)]
    counter = HistoricalAnalogueCounter(connection, schema_version_id)

    counts = counter.count_analogues(securities, AS_OF, _batch(securities, BASE_SHAPE))
    assert set(counts) == set(securities)


def test_a_security_without_a_feature_vector_is_a_recorded_zero(
    connection: Connection, register, schema_version_id: UUID
):
    """Not an omission, and not an exception — a zero with a stated reason."""
    present, absent = register("HASVEC"), register("NOVEC")
    counter = HistoricalAnalogueCounter(connection, schema_version_id)

    counts = counter.count_analogues([present, absent], AS_OF, _batch([present], BASE_SHAPE))

    assert counts[absent].count == 0
    assert counts[absent].detail["reason"] == "no_feature_vector"


def test_an_empty_security_list_returns_an_empty_mapping(
    connection: Connection, schema_version_id: UUID
):
    counter = HistoricalAnalogueCounter(connection, schema_version_id)
    assert counter.count_analogues([], AS_OF, _batch([], BASE_SHAPE)) == {}


# --------------------------------------------------------------------------
# Cross-asset only, and the honest zero
# --------------------------------------------------------------------------


def test_the_counter_counts_cross_asset_only(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """A security's own history is not a statistical base for this gate.

    Module 09's gate asks whether there are enough *comparable past
    instances* to make a claim. Three prior attempts by the same company
    are its own history — reported separately by this module — and
    counting them toward a cross-sectional floor would let a repeat
    failure qualify itself.
    """
    subject = register("SELFONLY")
    for index in range(6):
        make_case(
            subject,
            _features(BASE_SHAPE, jitter=0.01 * index, seed=index),
            detected_at=datetime(2022, 1, 10, tzinfo=UTC) + timedelta(days=60 * index),
        )

    counter = HistoricalAnalogueCounter(connection, schema_version_id, config=_wide_config())
    counts = counter.count_analogues([subject], AS_OF, _batch([subject], BASE_SHAPE))

    assert counts[subject].count == 0, "the subject's own six cases must not count"
    assert counts[subject].detail["scope"] == "CROSS_ASSET"


def test_an_empty_case_dataset_returns_zero_for_everything(
    connection: Connection, register, schema_version_id: UUID
):
    """Today's reality, asserted so it cannot surprise anyone later.

    Before Module 17's scan populates the case dataset this counter
    returns 0 for every security, so enabling the gate with any positive
    threshold gates the entire universe out. Arithmetic, not a defect —
    but it means the swap should wait for the dataset.
    """
    securities = [register(f"EMPTY{i}") for i in range(3)]
    counter = HistoricalAnalogueCounter(connection, schema_version_id)

    counts = counter.count_analogues(securities, AS_OF, _batch(securities, BASE_SHAPE))

    assert all(c.count == 0 for c in counts.values())
    assert all(c.count < RE_DERIVED_MIN_ANALOGUES for c in counts.values())


# --------------------------------------------------------------------------
# Re-derivation
# --------------------------------------------------------------------------


def test_the_threshold_was_re_derived_not_inherited():
    """Module 09's 5 was calibrated against a counter measuring something else.

    The placeholder counted cross-sectional peers in a 400-security
    universe — tens to hundreds. This counts concluded historical setups
    within a distance radius. Carrying the number across would have been
    a category error, not a rescaling.
    """
    from core.candidate_detection.config import EligibilityParameters as Module09

    assert Module09().min_historical_analogues != RE_DERIVED_MIN_ANALOGUES
    assert RE_DERIVED_MIN_ANALOGUES == 15


def test_the_re_derived_threshold_matches_its_stated_reasoning():
    """15 is where a proportion's interval stops spanning everything.

    The threshold was derived from Wilson interval widths rather than from
    data, so the arithmetic behind it is checkable — and if someone
    changes the number without changing the reasoning, this fails.
    """
    from core.historical_similarity.statistics import wilson_interval

    at_five = wilson_interval(2, 5).width
    at_derived = wilson_interval(RE_DERIVED_MIN_ANALOGUES // 2, RE_DERIVED_MIN_ANALOGUES).width
    at_thirty = wilson_interval(15, 30).width

    assert at_five > 0.6, "a 5-case proportion says essentially nothing"
    assert at_derived < 0.55
    assert at_thirty < at_derived


def test_results_stay_marked_provisional(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """The method is real; the radius and the dataset are not validated.

    Marking these non-provisional would claim a validation that has not
    happened — and Module 09 stores the flag, so a stored gate result
    would then misrepresent itself permanently.
    """
    subject = register("PROVISIONAL")
    make_case(register("ACASE"), _features(BASE_SHAPE, seed=1))

    counter = HistoricalAnalogueCounter(connection, schema_version_id, config=_wide_config())
    count = counter.count_analogues([subject], AS_OF, _batch([subject], BASE_SHAPE))[subject]

    assert count.provisional is True
    assert count.method == "historical_similarity_v1"
    assert count.detail["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"


def test_the_counter_loads_the_case_set_once_for_the_whole_batch(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """Batched, not per-security — the discipline Modules 08-10 follow."""
    from sqlalchemy import event

    make_case(register("BATCHCASE"), _features(BASE_SHAPE, seed=1))
    securities = [register(f"BATCHSUB{i}") for i in range(10)]

    counter = HistoricalAnalogueCounter(connection, schema_version_id, config=_wide_config())
    seen = {"count": 0}

    def listener(*_a: object, **_k: object) -> None:
        seen["count"] += 1

    event.listen(connection, "before_cursor_execute", listener)
    try:
        counter.count_analogues(securities, AS_OF, _batch(securities, BASE_SHAPE))
    finally:
        event.remove(connection, "before_cursor_execute", listener)

    # One case load, plus the per-security transition-history reads that
    # `find_similar_setups` performs for same-asset facts. The case set —
    # the expensive query — is loaded once.
    assert seen["count"] < 3 * len(securities), (
        f"{seen['count']} queries for {len(securities)} securities suggests "
        "the case set is being reloaded per security"
    )


@pytest.mark.parametrize("shape", [BASE_SHAPE, MOMENTUM_SHAPE])
def test_the_counter_works_for_any_candidate_shape(
    connection: Connection, register, schema_version_id: UUID, shape: dict
):
    """No shape produces an exception or an omission."""
    subject = register(f"SHAPE{abs(hash(str(sorted(shape.items())))) % 1000}")
    counter = HistoricalAnalogueCounter(connection, schema_version_id)

    counts = counter.count_analogues([subject], AS_OF, _batch([subject], shape))
    assert counts[subject].count == 0
