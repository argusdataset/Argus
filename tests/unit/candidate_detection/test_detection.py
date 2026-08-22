"""Does the coarse pre-filter reduce the universe without losing the point?

Two failure modes, pulling in opposite directions, and both are tested:

- A filter that barely reduces the universe has not earned its place —
  every downstream stage then runs on 10,000 securities anyway.
- A filter that reduces it by dropping the basing securities has actively
  destroyed ARGUS. A high-recall stage that misses real candidates is
  worse than no stage at all, because the miss is silent and permanent.

So the population-level assertion (order-of-magnitude reduction) is always
paired with an individual-level one (the planted base is still there).
"""

from __future__ import annotations

import pytest

from core.candidate_detection import DetectionConfig, detect_candidates
from core.candidate_detection.config import DetectionParameters
from core.candidate_detection.detection import RANKING_COMPONENTS
from core.candidate_detection.pool import ExclusionReason
from core.feature_engine.vector import BatchFeatureResult
from data.canonical_model.records import CanonicalTimeframe
from tests.unit.candidate_detection.synthetic import (
    AT_HIGHS_ID,
    BASING_ID,
    SPARSE_ID,
    THIN_SMALLCAP_ID,
    UNTRADEABLE_ID,
    build_universe,
)


@pytest.fixture(scope="module")
def universe() -> BatchFeatureResult:
    return build_universe()


@pytest.fixture(scope="module")
def pool(universe: BatchFeatureResult):
    return detect_candidates(universe)


# --------------------------------------------------------------------------
# Reduction
# --------------------------------------------------------------------------


def test_the_pool_is_an_order_of_magnitude_smaller_than_the_universe(pool, universe):
    """The headline claim, stated as a ratio rather than a count."""
    assert pool.considered == len(universe.vectors)
    assert pool.reduction_ratio <= 0.15
    assert len(pool) < len(universe.vectors) / 5


def test_the_pool_is_not_empty(pool):
    """A filter that returns nothing is a reduction, and a useless one."""
    assert len(pool) > 0


def test_every_security_is_either_a_candidate_or_has_a_recorded_reason(pool, universe):
    """Nothing vanishes. A filter that discards its own record cannot be audited.

    The same discipline as Module 08's `missing_securities`: a caller who
    asked about 400 securities must be able to account for all 400.
    """
    accounted = set(pool.candidates) | set(pool.excluded)
    assert accounted == set(universe.vectors)


def test_the_selection_fraction_controls_the_pool_size(universe):
    """The knob is a compute budget, and it demonstrably behaves like one."""
    small = detect_candidates(
        universe, config=DetectionConfig(detection=DetectionParameters(selection_fraction=0.02))
    )
    large = detect_candidates(
        universe, config=DetectionConfig(detection=DetectionParameters(selection_fraction=0.25))
    )
    assert len(small) < len(large)
    assert small.reduction_ratio == pytest.approx(0.02, abs=0.01)
    assert large.reduction_ratio == pytest.approx(0.25, abs=0.02)


def test_a_tiny_universe_still_yields_at_least_one_candidate(universe):
    """Rounding a fraction to zero would silently stop the whole pipeline."""
    tiny = BatchFeatureResult(
        as_of=universe.as_of,
        feature_schema_version_id=None,
        timeframe=CanonicalTimeframe.DAILY,
        vectors={BASING_ID: universe.vectors[BASING_ID]},
        missing_securities={},
    )
    assert len(detect_candidates(tiny)) == 1


# --------------------------------------------------------------------------
# Recall — the expensive error
# --------------------------------------------------------------------------


def test_the_textbook_base_reaches_the_pool(pool):
    """If this fails, nothing else about the module matters."""
    assert BASING_ID in pool


def test_the_thinly_traded_small_cap_reaches_the_pool(pool):
    """Detection must not pre-empt the liquidity gate.

    The MLSS/SLS/QBTS profile: a real base on a microcap tape. Whether it
    is tradeable is the eligibility stage's question, and answering it
    here would blend the two decisions Module 09 exists to keep apart.
    """
    assert THIN_SMALLCAP_ID in pool


def test_even_an_untradeable_security_reaches_the_pool(pool):
    """Detection asks about the pattern, not about tradeability.

    This looks like a bug and is the design. The security is rejected a
    stage later, by the gate that owns that question, with a recorded
    reason — which is what makes "we rejected it for liquidity" a fact you
    can count rather than an invisible side effect of the pre-filter.
    """
    assert UNTRADEABLE_ID in pool


def test_a_sparse_security_is_not_pre_emptively_excluded(pool):
    """Missing data is the DATA_QUALITY gate's call, not detection's.

    A recently-listed security whose available features look base-like
    ranks into the pool. Excluding it here would make a data gap
    indistinguishable from a pattern miss in every subsequent
    false-negative analysis.
    """
    assert SPARSE_ID in pool


# --------------------------------------------------------------------------
# The one directional requirement
# --------------------------------------------------------------------------


def test_a_security_at_its_highs_is_excluded_before_ranking(pool):
    """The ARGUS setup begins with a prior decline. No decline, no setup."""
    assert AT_HIGHS_ID not in pool
    assert pool.excluded[AT_HIGHS_ID] is ExclusionReason.NOT_OFF_ITS_PEAK


def test_an_unmeasurable_drawdown_is_not_treated_as_being_at_highs(pool):
    """NaN is not a pattern verdict.

    A security whose drawdown could not be computed is not thereby known
    to be at its highs, and turning the data gap into an exclusion would
    be exactly the "silently wrong number" failure Module 08 was built to
    prevent — one stage later.
    """
    assert pool.excluded.get(SPARSE_ID) is not ExclusionReason.NOT_OFF_ITS_PEAK


def test_securities_with_too_few_components_are_reported_not_ranked(universe):
    """A composite built from one component is not a composite."""
    stripped = dict(universe.vectors)
    victim = next(iter(stripped))
    vector = stripped[victim]
    stripped[victim] = type(vector)(
        security_id=vector.security_id,
        as_of=vector.as_of,
        feature_schema_version_id=None,
        timeframe=vector.timeframe,
        event_time=vector.event_time,
        availability_time=vector.availability_time,
        features={**vector.features, **dict.fromkeys(RANKING_COMPONENTS)},
        evidence=vector.evidence,
    )
    result = detect_candidates(
        BatchFeatureResult(
            as_of=universe.as_of,
            feature_schema_version_id=None,
            timeframe=CanonicalTimeframe.DAILY,
            vectors=stripped,
            missing_securities={},
        )
    )
    assert result.excluded[victim] is ExclusionReason.INSUFFICIENT_COMPONENTS


def test_securities_module_08_could_not_compute_are_carried_through(universe):
    """`missing_securities` becomes `NO_FEATURES`, not silence."""
    from uuid import uuid4

    from core.data_validation.result import MissReason

    absent = uuid4()
    result = detect_candidates(
        BatchFeatureResult(
            as_of=universe.as_of,
            feature_schema_version_id=None,
            timeframe=CanonicalTimeframe.DAILY,
            vectors=universe.vectors,
            missing_securities={absent: MissReason.NOT_YET_AVAILABLE},
        )
    )
    assert result.excluded[absent] is ExclusionReason.NO_FEATURES


# --------------------------------------------------------------------------
# Explainability
# --------------------------------------------------------------------------


def test_every_candidate_carries_the_components_that_selected_it(pool):
    """A pool entry ARGUS cannot explain is a pool entry it should not make."""
    for candidate in pool.ranked():
        assert candidate.components, "a candidate with no named components is unexplainable"
        assert set(candidate.components) <= set(RANKING_COMPONENTS)
        assert all(0.0 <= rank <= 1.0 for rank in candidate.components.values())


def test_the_composite_rank_orders_the_pool(pool):
    ranked = pool.ranked()
    assert ranked == sorted(ranked, key=lambda c: c.composite_rank, reverse=True)


def test_the_textbook_base_outranks_most_of_the_pool(pool):
    """The ranking is doing real work, not just cutting arbitrarily.

    Not "ranks first" — the background deliberately contains basing-like
    securities that should compete — but a textbook base landing in the
    bottom half of the pool would mean the composite is not measuring what
    it claims to.
    """
    ranked = pool.ranked()
    position = [c.security_id for c in ranked].index(BASING_ID)
    assert position < len(ranked) / 2


# --------------------------------------------------------------------------
# Determinism and I/O
# --------------------------------------------------------------------------


def test_detection_is_deterministic(universe):
    """Same input, same pool — a prerequisite for reproducible replay."""
    first = detect_candidates(universe, run_id=None)
    second = detect_candidates(universe, run_id=None)
    assert set(first.candidates) == set(second.candidates)


def test_detection_performs_no_io(universe):
    """It takes feature vectors, not a connection.

    Asserted structurally: a detection stage that queried the database
    would make "is this vectorized" unanswerable by reading it, and would
    couple the pre-filter to a schema it has no business knowing about.
    """
    import inspect

    from core.candidate_detection import detection

    signature = inspect.signature(detection.detect_candidates)
    assert "connection" not in signature.parameters
    assert "Connection" not in inspect.getsource(detection)
