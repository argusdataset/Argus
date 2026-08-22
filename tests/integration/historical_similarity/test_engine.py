"""The engine end to end: separation, PIT, sparse data, and persistence.

The separation tests are the load-bearing ones. Cross-asset and same-asset
are different kinds of evidence, and a security's own three prior attempts
must never be averaged into the crowd — a company that failed this pattern
twice is not "due" for success, and blending would make that signal
disappear.

The tests therefore check not only that the two results differ, but that
there is **no combined number anywhere** to reach for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.historical_similarity import (
    SampleSufficiency,
    SimilarityConfig,
    SimilarityThreshold,
    SimilarityThresholds,
    find_similar_setups,
    insufficient,
    load_cases,
    publish_similarity_configuration,
    write_similarity_results,
)
from core.historical_similarity.features import metric_features
from infra.db.enums import AnalogueScope, MarketState, OutcomeStatus
from infra.db.schema.intelligence import historical_similarity_results

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)

#: The ARGUS target shape, across the features the metric compares.
#:
#: Deliberately broad rather than pinning six or seven values. A shape
#: defined on a handful of features leaves the rest identical between
#: profiles, and the metric's mean-over-shared-features normalization then
#: dilutes the few genuine differences — a fixture artifact that made an
#: unrelated momentum name land inside the radius during development. Real
#: securities differ across the whole vector, so the fixtures do too.
BASE_SHAPE = {
    # Group A — a deep decline that has run its course
    "drawdown_pct": -0.70,
    "peak_to_trough_decline": -0.76,
    "decline_duration_bars": 180.0,
    "decline_speed": -0.004,
    "distance_from_historical_high": -0.74,
    "lower_low_frequency": 0.05,
    "downside_momentum_reduction": 0.30,
    "momentum_deterioration": 0.02,
    # Group B — coiled and quiet
    "volatility_compression": 0.36,
    "atr_percentile": 0.06,
    "normalized_range_width": 0.07,
    "realized_volatility": 0.28,
    "volume_contraction": 0.44,
    "rvol": 0.60,
    "support_test_count": 5.0,
    "resistance_test_count": 4.0,
    "higher_low_development": 0.001,
    "structure_transition": 0.05,
    # Group C/D — not yet moving
    "volatility_reexpansion": 0.85,
    "volume_expansion": 0.90,
    "resistance_pressure": 0.45,
    "time_in_upper_range": 0.20,
    "higher_high_frequency": 0.10,
    "resistance_breakout_pct": -0.06,
    "acceptance_followthrough": 0.0,
    # Group E
    "rs_vs_market": -0.55,
}

#: A momentum name at its highs — the opposite structure on every axis.
MOMENTUM_SHAPE = {
    "drawdown_pct": -0.02,
    "peak_to_trough_decline": -0.05,
    "decline_duration_bars": 4.0,
    "decline_speed": -0.012,
    "distance_from_historical_high": -0.01,
    "lower_low_frequency": 0.0,
    "downside_momentum_reduction": -0.10,
    "momentum_deterioration": 0.25,
    "volatility_compression": 1.85,
    "atr_percentile": 0.94,
    "normalized_range_width": 0.38,
    "realized_volatility": 0.95,
    "volume_contraction": 1.75,
    "rvol": 2.10,
    "support_test_count": 0.0,
    "resistance_test_count": 0.0,
    "higher_low_development": 0.020,
    "structure_transition": 0.90,
    "volatility_reexpansion": 1.90,
    "volume_expansion": 2.00,
    "resistance_pressure": 0.97,
    "time_in_upper_range": 0.85,
    "higher_high_frequency": 0.70,
    "resistance_breakout_pct": 0.09,
    "acceptance_followthrough": 0.80,
    "rs_vs_market": 0.60,
}


def _shape_background(shape_name: str) -> dict[str, float]:
    """Shape-specific values for every feature the shape does not pin.

    Real feature vectors have a value for all 41 features, and those values
    differ systematically between structurally different securities. A
    fixture that left them as shared noise would give every pair the same
    contribution from those features — a constant floor added to every
    distance, compressing the difference between good and bad matches.

    That floor is not a fixture artifact alone: it is the standard argument
    for feature selection, and the reason `metric_features()` excludes
    liquidity, sector and absolute-scale features rather than letting them
    dilute the comparison.
    """
    rng = np.random.default_rng(abs(hash(shape_name)) % (2**32))
    return {name: float(rng.normal(0.0, 0.6)) for name in metric_features()}


def _features(shape: dict[str, float], jitter: float = 0.0, *, seed: int | None = None) -> dict:
    """A full feature payload from a named shape.

    The shape's own defining features are pinned; everything else takes a
    value characteristic of that shape. `jitter` shifts the defining
    features to build a near-twin; `seed` adds small per-case variation so
    the case population is not degenerate.
    """
    shape_name = "base" if shape is BASE_SHAPE else "momentum"
    values = _shape_background(shape_name)
    values.update({k: v + jitter for k, v in shape.items()})

    if seed is not None:
        rng = np.random.default_rng(seed)
        for name in values:
            values[name] += float(rng.normal(0.0, 0.01))

    # Module 08 stores its evidence block alongside; included so the
    # loader's stripping of it is genuinely exercised.
    values["_evidence"] = {"bars_available": 800, "bars_required": 252}
    return values


def _population(make_case, register, count: int, shape: dict[str, float], prefix: str, **kw):
    """Seed `count` cases of one shape, each with its own variation."""
    for index in range(count):
        make_case(
            register(f"{prefix}{index}"),
            _features(shape, jitter=0.01 * index, seed=index),
            **kw,
        )


def _contrast(make_case, register, count: int = 4):
    """Contrasting cases so the population has real spread.

    Without these the median absolute deviations are estimated from a set
    of near-identical vectors, collapse toward zero, and every distance
    inflates. A real case set contains varied setups.
    """
    _population(make_case, register, count, MOMENTUM_SHAPE, "CONTRAST")


#: A radius chosen for these fixtures rather than the shipped default.
#:
#: The shipped 1.25 is an explicitly unvalidated placeholder, so a test
#: asserting "exactly N cases fall inside it" would really be asserting
#: that my synthetic shapes happen to straddle an invented number — it
#: would break the moment the radius is recalibrated against real data,
#: for no reason connected to the behaviour under test.
#:
#: So counting tests pin the radius explicitly, and the metric's real
#: claim — that base-shaped cases rank ahead of unrelated ones — is
#: tested as an ORDERING, which survives any recalibration.
def _config(max_distance: float) -> SimilarityConfig:
    default = SimilarityThresholds()
    return SimilarityConfig(
        thresholds=SimilarityThresholds(
            max_distance=SimilarityThreshold(
                value=max_distance,
                kind=default.max_distance.kind,
                rationale="pinned by the test fixture; see module docstring",
            )
        )
    )


#: Comfortably admits base-shaped cases, excludes momentum-shaped ones in
#: a balanced population. Verified by
#: `test_the_fixture_radius_separates_the_two_shapes`.
FIXTURE_RADIUS = 0.90

#: Admits everything, so a test can observe the full ranked population.
#:
#: Used by every test that is not *about* the radius — which is most of
#: them. Relative scaling makes an absolute distance depend on the shape
#: of the whole case population: the interquartile range only spans the
#: gap between two clusters when the split is near even, so in a
#: population of one base-shaped case and four momentum-shaped ones the
#: scales collapse to within-cluster noise and every distance inflates.
#:
#: That is a real property of the metric worth knowing (it is documented
#: in `distance.py`), and it is a terrible thing to build unrelated tests
#: on top of. Separation, statistics and point-in-time behaviour are all
#: claims that hold regardless of radius, so those tests admit everything
#: and assert on scopes and ordering instead.
ADMIT_EVERYTHING = 1_000.0


@pytest.fixture
def snapshot_id(connection: Connection) -> UUID:
    return publish_similarity_configuration(connection, SimilarityConfig(), as_of=AS_OF)


# --------------------------------------------------------------------------
# Cross-asset and same-asset stay separate
# --------------------------------------------------------------------------


def test_a_securitys_own_cases_never_enter_its_cross_asset_result(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """The most important filter in the module.

    Leaving a security's own history in its cross-asset comparison would
    let it corroborate itself, and the whole cross/same separation exists
    because those are different kinds of evidence.
    """
    subject = register("SUBJECT")
    for index in range(4):
        # Staggered: a security's repeat attempts at this pattern happen
        # months apart, and Module 08's unique constraint on
        # (security, schema version, event_time) rightly refuses to let
        # one security hold two vectors for the same instant.
        make_case(
            subject,
            _features(BASE_SHAPE, jitter=0.01 * index, seed=index),
            detected_at=datetime(2022, 1, 10, tzinfo=UTC) + timedelta(days=90 * index),
        )
    _population(make_case, register, 6, BASE_SHAPE, "PEER")

    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )

    cross_securities = {m.security_id for m in evidence.cross_asset.matches}
    same_securities = {m.security_id for m in evidence.same_asset.matches}

    assert subject not in cross_securities
    assert same_securities == {subject}
    assert evidence.cross_asset.count == 6, "the six peers, and nothing of the subject's"
    assert evidence.same_asset.count == 4


def test_the_two_scopes_carry_independent_statistics(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """Different outcomes in each scope must produce different numbers.

    The subject failed every prior attempt; its peers succeeded. Blending
    would average those into something true of neither.
    """
    subject = register("REPEATER")
    for index in range(6):
        make_case(
            subject,
            _features(BASE_SHAPE, jitter=0.01 * index, seed=index),
            detected_at=datetime(2022, 1, 10, tzinfo=UTC) + timedelta(days=60 * index),
            outcome_status=OutcomeStatus.FAILED,
            realized_return=-0.30,
        )
    _population(
        make_case,
        register,
        8,
        BASE_SHAPE,
        "WINNER",
        outcome_status=OutcomeStatus.SUCCESS,
        realized_return=0.40,
    )

    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )

    assert evidence.cross_asset.statistics.failure_rate == pytest.approx(0.0)
    assert evidence.same_asset.statistics.failure_rate == pytest.approx(1.0)
    assert evidence.cross_asset.statistics.median_outcome == pytest.approx(0.40)
    assert evidence.same_asset.statistics.median_outcome == pytest.approx(-0.30)


def test_there_is_no_combined_count_or_blended_statistic(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """Structural: the result type offers nothing to blend with.

    Not merely that the two numbers differ — that no combined number
    exists to be reached for. The moment one does, downstream code will
    use it and the distinction is gone.
    """
    subject = register("STRUCTURAL")
    make_case(subject, _features(BASE_SHAPE))
    make_case(register("OTHER"), _features(BASE_SHAPE))

    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )

    fields = set(type(evidence).__dataclass_fields__)
    assert "cross_asset" in fields and "same_asset" in fields
    for forbidden in ("combined", "total_count", "blended", "overall_similarity"):
        assert forbidden not in fields
    assert not hasattr(evidence, "similarity_score")


def test_the_scopes_are_stored_as_separate_rows(
    connection: Connection, register, make_case, schema_version_id: UUID, snapshot_id: UUID
):
    """Module 03's unique constraint enforces in SQL what the types enforce
    in Python: the two scopes cannot occupy one row."""
    subject = register("STORED")
    make_case(subject, _features(BASE_SHAPE))
    make_case(register("PEER"), _features(BASE_SHAPE))

    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        data_snapshot_id=snapshot_id,
    )
    assert write_similarity_results(connection, evidence) == 2

    scopes = (
        connection.execute(
            select(historical_similarity_results.c.scope).where(
                historical_similarity_results.c.security_id == subject
            )
        )
        .scalars()
        .all()
    )
    assert sorted(scopes) == [AnalogueScope.CROSS_ASSET.value, AnalogueScope.SAME_ASSET.value]


# --------------------------------------------------------------------------
# Ranking on real stored cases
# --------------------------------------------------------------------------


def test_matching_shapes_rank_above_unrelated_ones(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """The metric's real claim, tested as an ordering.

    Asserted as a rank rather than as radius membership: which cases fall
    inside an invented radius is a statement about the radius, and the
    radius is an unvalidated placeholder. That every base-shaped case is
    closer than every momentum-shaped one is a statement about the metric,
    and survives recalibration.
    """
    subject = register("QUERY")
    twins = [register(f"TWIN{i}") for i in range(3)]
    for index, twin in enumerate(twins):
        make_case(twin, _features(BASE_SHAPE, jitter=0.01 * index, seed=index))
    unrelated = [register(f"UNREL{i}") for i in range(3)]
    for index, other in enumerate(unrelated):
        make_case(other, _features(MOMENTUM_SHAPE, jitter=0.01 * index, seed=index))

    # A radius wide enough to admit everything, so the ordering over the
    # whole population is observable rather than pre-filtered.
    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )
    ranked = [m.security_id for m in evidence.cross_asset.matches]

    assert len(ranked) == 6
    positions = {sid: ranked.index(sid) for sid in ranked}
    assert max(positions[t] for t in twins) < min(positions[u] for u in unrelated), (
        "every base-shaped case must rank ahead of every unrelated one"
    )


def test_the_fixture_radius_separates_the_two_shapes(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """Pins the fixture premise the counting tests rely on.

    If the shapes ever stop straddling FIXTURE_RADIUS, the counting tests
    would start failing for reasons unrelated to what they test. This
    fails first, and says why.
    """
    subject = register("RADIUS")
    for index in range(3):
        make_case(register(f"RB{index}"), _features(BASE_SHAPE, jitter=0.01 * index, seed=index))
    for index in range(3):
        make_case(
            register(f"RM{index}"), _features(MOMENTUM_SHAPE, jitter=0.01 * index, seed=index)
        )

    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(FIXTURE_RADIUS),
    )
    assert evidence.cross_asset.count == 3, (
        "FIXTURE_RADIUS must admit the three base-shaped cases and no others"
    )


def test_every_match_explains_itself(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """ "Why is this similar" answered by naming features — the MVP constraint."""
    subject = register("EXPLAIN")
    make_case(register("NEIGHBOUR"), _features(BASE_SHAPE, jitter=0.01, seed=1))

    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )
    match = evidence.cross_asset.matches[0]

    assert match.contributions
    assert all(c.feature in set(metric_features()) for c in match.contributions)
    assert match.as_dict()["closest_features"]


# --------------------------------------------------------------------------
# Sparse data — the normal case before Module 17 runs
# --------------------------------------------------------------------------


def test_an_empty_case_dataset_yields_an_honest_insufficient_result(
    connection: Connection, register, schema_version_id: UUID
):
    """Today's reality: no cases exist, and the answer says so.

    Not a crash, not a zero dressed as a finding — a count of 0 and an
    explicit INSUFFICIENT with every statistic None.
    """
    subject = register("NOCASES")
    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )

    assert evidence.cross_asset.count == 0
    assert evidence.same_asset.count == 0
    for scope in (evidence.cross_asset, evidence.same_asset):
        assert scope.statistics.sufficiency is SampleSufficiency.INSUFFICIENT
        assert scope.statistics.median_outcome is None
        assert scope.statistics.failure_rate is None
    assert insufficient(evidence)


def test_three_analogues_produce_a_count_but_no_statistics(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """The concrete small-sample example.

    Three matching cases: the count is reported because it is a fact about
    the search; the median is not, because three points cannot support it.
    """
    subject = register("THREE")
    _population(make_case, register, 3, BASE_SHAPE, "CASE")

    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )

    assert evidence.cross_asset.count == 3
    assert evidence.cross_asset.statistics.sufficiency is SampleSufficiency.INSUFFICIENT
    assert evidence.cross_asset.statistics.median_outcome is None


def test_sparse_results_are_flagged_when_they_do_report(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """Between the floors: numbers plus a warning label."""
    subject = register("SPARSE")
    _population(make_case, register, 8, BASE_SHAPE, "S")

    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )

    assert evidence.cross_asset.statistics.sufficiency is SampleSufficiency.SPARSE
    assert evidence.cross_asset.statistics.median_outcome is not None


def test_no_analogues_and_no_comparable_cases_are_distinguished(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """ "Nothing was similar" and "nothing was comparable" are different facts."""
    subject = register("DISTINCT")
    make_case(register("TOOFAR"), _features(MOMENTUM_SHAPE, seed=1))

    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(FIXTURE_RADIUS),
    )

    # A momentum-shaped case is comparable — it shares every feature — it
    # is simply too far. That is a different fact from "not enough shared
    # features to compute a distance", and the two must not collapse.
    assert evidence.cross_asset.count == 0
    assert evidence.cross_asset.considered == 1
    assert evidence.cross_asset.incomparable == 0, "it was comparable, just far"


# --------------------------------------------------------------------------
# Point-in-time correctness
# --------------------------------------------------------------------------


def test_a_case_that_had_not_concluded_by_as_of_is_invisible(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """The leak this module could introduce.

    Comparing today's candidate against a setup whose outcome was recorded
    *after* `as_of` would let the future inform the present — through a
    table Module 07's `get_as_of` never sees.
    """
    subject = register("PITSUB")
    make_case(
        register("FUTURE"),
        _features(BASE_SHAPE, seed=1),
        recorded_at=datetime(2025, 1, 1, tzinfo=UTC),
    )

    invisible = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )
    visible = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        datetime(2025, 6, 1, tzinfo=UTC),
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )

    assert invisible.cross_asset.count == 0
    assert visible.cross_asset.count == 1


def test_a_case_uses_the_features_knowable_at_its_own_detection(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """The second, subtler cutoff.

    A feature vector that only became available after the setup was
    detected must not be the one compared against — that would compare
    today's candidate to a version of history including later
    restatements.
    """
    peer = register("LATEVEC")
    detected = datetime(2023, 1, 10, tzinfo=UTC)
    make_case(
        peer,
        _features(BASE_SHAPE),
        detected_at=detected,
        feature_availability=detected + timedelta(days=30),
    )

    cases = load_cases(connection, AS_OF, schema_version_id)
    assert cases.is_empty, "a vector available only after detection is not usable"


def test_the_same_call_searches_a_different_era_with_a_different_as_of(
    connection: Connection, register, make_case, schema_version_id: UUID
):
    """Dual mode: one function, one argument different."""
    subject = register("DUAL")
    make_case(
        register("EARLY"),
        _features(BASE_SHAPE, seed=1),
        recorded_at=datetime(2022, 6, 1, tzinfo=UTC),
    )
    make_case(
        register("LATE"),
        _features(BASE_SHAPE, jitter=0.01, seed=2),
        recorded_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    early = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        datetime(2023, 1, 1, tzinfo=UTC),
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )
    late = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )

    assert early.cross_asset.count == 1
    assert late.cross_asset.count == 2


# --------------------------------------------------------------------------
# Same-asset transition history
# --------------------------------------------------------------------------


def test_same_asset_history_reads_module_10s_transition_facts(
    connection: Connection, register, make_case, schema_version_id: UUID, version_ids
):
    """Facts only. Module 10's confidence is deliberately not consulted."""
    from core.market_state.classifier import ClassificationResult, StateAssignment
    from core.market_state.transitions import record_transitions

    subject = register("HISTORY")
    for index, state in enumerate(
        [
            MarketState.CONSOLIDATION,
            MarketState.BREAKOUT_WATCH,
            MarketState.CONSOLIDATION,
            MarketState.BREAKOUT_WATCH,
        ]
    ):
        record_transitions(
            connection,
            ClassificationResult(
                as_of=datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=30 * index),
                target_model_version_id=version_ids["target_model"],
                assignments={
                    subject: StateAssignment(
                        security_id=subject,
                        state=state,
                        # A confidence that must be ignored downstream.
                        confidence=0.95,
                        evidence={},
                    )
                },
            ),
        )

    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )
    history = evidence.same_asset_history

    assert history.cycle_counts["BREAKOUT_WATCH"] == 2
    assert history.cycle_counts["CONSOLIDATION"] == 2
    assert history.backward_transitions == 1
    assert "confidence" not in history.as_dict()


def test_same_asset_history_is_bounded_by_as_of(
    connection: Connection, register, schema_version_id: UUID, version_ids
):
    """A replay must not see cycles that had not happened yet."""
    from core.market_state.classifier import ClassificationResult, StateAssignment
    from core.market_state.transitions import record_transitions

    subject = register("BOUNDED")
    for index, state in enumerate([MarketState.CONSOLIDATION, MarketState.BREAKOUT_WATCH]):
        record_transitions(
            connection,
            ClassificationResult(
                as_of=datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=400 * index),
                target_model_version_id=version_ids["target_model"],
                assignments={
                    subject: StateAssignment(
                        security_id=subject, state=state, confidence=None, evidence={}
                    )
                },
            ),
        )

    early = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        datetime(2023, 6, 1, tzinfo=UTC),
        schema_version_id,
    )
    assert early.same_asset_history.cycle_counts["BREAKOUT_WATCH"] == 0


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_an_unstamped_result_is_refused(connection: Connection, register, schema_version_id: UUID):
    """No snapshot, no write — the radius must stay attributable."""
    subject = register("UNSTAMPED")
    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        config=_config(ADMIT_EVERYTHING),
    )

    with pytest.raises(ValueError, match="data_snapshot_id"):
        write_similarity_results(connection, evidence)


def test_a_scope_with_no_analogues_still_gets_a_row(
    connection: Connection, register, schema_version_id: UUID, snapshot_id: UUID
):
    """ "We searched and found nothing" and "we never searched" differ."""
    subject = register("EMPTYROW")
    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        data_snapshot_id=snapshot_id,
    )

    assert write_similarity_results(connection, evidence) == 2
    counts = (
        connection.execute(
            select(historical_similarity_results.c.similar_setup_count).where(
                historical_similarity_results.c.security_id == subject
            )
        )
        .scalars()
        .all()
    )
    assert counts == [0, 0]


def test_stored_results_say_their_thresholds_are_unvalidated(
    connection: Connection, register, schema_version_id: UUID, snapshot_id: UUID
):
    """Recorded in the database, not only in a docstring."""
    subject = register("CALIB")
    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        data_snapshot_id=snapshot_id,
    )
    write_similarity_results(connection, evidence)

    distribution = (
        connection.execute(
            select(historical_similarity_results.c.similarity_distribution).where(
                historical_similarity_results.c.security_id == subject
            )
        )
        .scalars()
        .first()
    )

    assert distribution["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"
    assert "excluded_features" in distribution
    assert "volatility_contraction_onset" in distribution["excluded_features"]["duplicate_signal"]


def test_rewriting_the_same_result_inserts_nothing(
    connection: Connection, register, schema_version_id: UUID, snapshot_id: UUID
):
    """Insert-only: a recorded result is what a decision cited."""
    subject = register("REWRITE")
    evidence = find_similar_setups(
        connection,
        subject,
        _features(BASE_SHAPE),
        AS_OF,
        schema_version_id,
        data_snapshot_id=snapshot_id,
    )

    assert write_similarity_results(connection, evidence) == 2
    assert write_similarity_results(connection, evidence) == 0


def test_publishing_the_same_configuration_twice_returns_one_snapshot(
    connection: Connection,
):
    first = publish_similarity_configuration(connection, SimilarityConfig(), as_of=AS_OF)
    second = publish_similarity_configuration(connection, SimilarityConfig(), as_of=AS_OF)
    assert first == second


def test_the_same_configuration_at_a_different_as_of_is_a_different_snapshot(
    connection: Connection,
):
    """The cutoff is part of the snapshot's identity.

    The same thresholds at a different `as_of` search a different dataset
    and must not share a snapshot row, or two genuinely different results
    would collide on the unique constraint.
    """
    today = publish_similarity_configuration(connection, SimilarityConfig(), as_of=AS_OF)
    earlier = publish_similarity_configuration(
        connection, SimilarityConfig(), as_of=datetime(2020, 1, 1, tzinfo=UTC)
    )
    assert today != earlier
