"""Scoring a realistic candidate set, end to end through Modules 08-12.

Everything upstream is run for real: bars go into the database, Module 08
computes features from them, Module 10 classifies and assesses the target
model, Module 11 searches an (empty) case set, Module 12 assembles risk
context, and Module 13 scores what comes out. Hand-built inputs would test
this module against my belief about the other four; this tests it against
what they actually produce.

**The expected result today is that almost nothing gets scored**, and this
file asserts that as correct rather than working around it. The historical
case dataset is empty until Module 17's scan runs, so Module 11 returns
`INSUFFICIENT` for every candidate, which removes more of the component
weight than the coverage floor permits. A run that produced confident
scores right now would mean something had gone wrong.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pandas as pd
import pytest
from sqlalchemy.engine import Connection

from core.feature_engine.engine import compute_features_batch
from core.historical_similarity.engine import find_similar_setups
from core.historical_similarity.statistics import SampleSufficiency
from core.market_state import MarketStateConfig, publish_target_model_version
from core.market_state.classifier import classify_states
from core.market_state.target_model_matching.models.target_model_v1 import (
    model as target_model_v1,
)
from core.risk_context.assessment import assess_risk_context
from core.risk_context.events import translate_earnings_event, write_material_events
from core.scoring.components import ScoringInputs
from core.scoring.engine import Lineage, decision_counts, score_candidates
from core.scoring.gating import ScoringDecision
from core.scoring.upstream import assessments_from_classification
from data.provider_adapters.fmp.models import EarningsEvent, FetchProvenance
from tests.integration.feature_engine.conftest import insert_bars  # noqa: F401

AS_OF = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)
#: Enough bars to fill Module 08's longest window.
BARS = 420
#: The series has to *end* at `as_of`, not merely contain enough bars
#: somewhere in the past: Module 08 loads a bounded lookback back from
#: `as_of`, so a run of bars that stops a year early fills only part of it
#: and half the features come back None for reasons that have nothing to
#: do with what is being tested.
START = pd.bdate_range(end=AS_OF.date(), periods=BARS, tz="UTC")[0].to_pydatetime()


def _base_and_awakening(seed: int) -> list[float]:
    """A decline into a long quiet base with a late lift — the shape ARGUS
    is looking for, so the candidates are realistic rather than noise."""
    # The phase boundaries are set so the decline, the base and the lift
    # all fall inside the ~322-bar window Module 08 actually loads back
    # from `as_of`, not merely inside the 420 bars written.
    closes: list[float] = []
    price = 100.0
    for step in range(BARS):
        if step < BARS * 0.55:
            drift, noise = -0.0035, 0.015
        elif step < BARS * 0.93:
            drift, noise = 0.0000, 0.004
        else:
            drift, noise = 0.0030, 0.010
        wobble = math.sin((step + seed * 7) / 9.0) * noise
        price *= 1.0 + drift + wobble
        closes.append(max(price, 1.0))
    return closes


@pytest.fixture
def universe(connection: Connection, register) -> dict[str, UUID]:
    """Twelve candidates and a market benchmark, all with real bars."""
    members: dict[str, UUID] = {}
    for index in range(12):
        ticker = f"CAND{index:02d}"
        security_id = register(ticker)
        insert_bars(
            connection,
            security_id,
            start=START,
            closes=_base_and_awakening(index),
            volume=400_000 + index * 90_000,
        )
        members[ticker] = security_id

    benchmark = register("SPY")
    insert_bars(
        connection,
        benchmark,
        start=START,
        closes=[100.0 * (1.0002**step) for step in range(BARS)],
        volume=50_000_000,
    )
    members["SPY"] = benchmark
    return members


def _ingest_calendar(connection: Connection, universe: dict[str, UUID]) -> None:
    """Give every candidate a known earnings date, through Module 12's own
    ingestion path.

    Without this, Module 12 reports event proximity as *undetermined* —
    correctly, since no calendar was ever fetched — and that alone refuses
    every candidate before the historical-evidence question is reached.
    A real ARGUS run has calendar coverage, so the realistic set gets it
    too, and the residual refusal is then purely the empty case dataset.
    """
    fetched = AS_OF - timedelta(days=30)
    provenance = FetchProvenance(
        endpoint="earnings_calendar", url_path="/earnings-calendar", fetched_at=fetched
    )
    for index, (ticker, security_id) in enumerate(sorted(universe.items())):
        if ticker == "SPY":
            continue
        write_material_events(
            connection,
            [
                translate_earnings_event(
                    EarningsEvent(
                        provenance=provenance,
                        symbol=ticker,
                        earnings_date=(AS_OF + timedelta(days=20 + index)).date(),
                    ),
                    security_id,
                )
            ],
        )


def _run(
    connection: Connection,
    universe: dict[str, UUID],
    lineage: Lineage,
    *,
    with_calendar: bool = True,
):
    """Modules 08 -> 10 -> 11 -> 12 -> 13, for real."""
    if with_calendar:
        _ingest_calendar(connection, universe)
    benchmark = universe["SPY"]
    candidates = [sid for ticker, sid in universe.items() if ticker != "SPY"]

    features = compute_features_batch(connection, candidates, AS_OF, market_security_id=benchmark)

    version_id = publish_target_model_version(connection, MarketStateConfig())
    states = classify_states(
        features,
        target_model=target_model_v1.TargetModelV1(),
        target_model_version_id=version_id,
    )

    assessments = assessments_from_classification(states)

    inputs = []
    for security_id in candidates:
        vector = features.vectors.get(security_id)
        similarity = (
            find_similar_setups(
                connection,
                security_id,
                vector.features,
                AS_OF,
                lineage.feature_schema_version_id,
                include_same_asset_history=False,
            )
            if vector is not None
            else None
        )
        inputs.append(
            ScoringInputs(
                risk=assess_risk_context(connection, security_id, as_of=AS_OF, features=vector),
                features=vector,
                assessment=assessments.get(security_id),
                similarity=similarity,
            )
        )

    return features, states, score_candidates(inputs, as_of=AS_OF, lineage=lineage)


def test_a_realistic_run_is_almost_entirely_insufficient_evidence(connection, universe, lineage):
    """The headline assertion, and it is an assertion of correctness.

    Not a single candidate should be scored, because not a single one has
    historical analogues to be scored against. The reason is recorded on
    every result, so a reader can tell this apart from a bug.
    """
    _features, _states, results = _run(connection, universe, lineage)
    counts = decision_counts(results)

    assert len(results) == 12
    assert counts[ScoringDecision.INSUFFICIENT_EVIDENCE.value] == 12
    assert counts[ScoringDecision.SCORED.value] == 0
    assert all(result.argus_score is None for result in results)
    assert all("weight" in result.verdict.reason for result in results)


def test_without_calendar_coverage_the_refusal_comes_even_earlier(connection, universe, lineage):
    """And for a different, more specific reason.

    With no earnings calendar ingested, Module 12 reports event proximity
    as undetermined rather than as "no event". That refuses the candidate
    on the risk input before the coverage arithmetic is reached — which is
    the correct order, because "we could not measure the risk" is a more
    useful thing to tell a reader than "coverage was 0.67".
    """
    _features, _states, results = _run(connection, universe, lineage, with_calendar=False)

    assert all(r.decision is ScoringDecision.INSUFFICIENT_EVIDENCE for r in results)
    assert all(r.verdict.detail["undetermined_risk_inputs"] == ["event_proximity"] for r in results)


def test_the_reason_is_the_empty_case_dataset_and_nothing_else(connection, universe, lineage):
    """Distinguishing "no historical evidence yet" from "these candidates
    are bad". Every other component measured fine."""
    _features, _states, results = _run(connection, universe, lineage)

    for result in results:
        unmeasured = result.verdict.detail["unmeasured_components"]
        assert "historical_evidence" in unmeasured, result.security_id
        assert result.components["market_regime"].value is not None
        assert result.components["volume_liquidity"].value is not None
        assert result.components["volatility_structure"].value is not None
        assert result.components["risk_reward"].value is not None

    # For the candidates Module 10's target model actually covers, the
    # empty case dataset is the *only* thing missing. The rest are in
    # states the model does not judge, so they legitimately have no
    # pattern quality either — a different absence, and visible as one.
    covered = [r for r in results if r.components["pattern_quality"].value is not None]
    assert covered, "the fixture must produce at least one covered candidate"
    for result in covered:
        assert result.verdict.detail["unmeasured_components"] == ["historical_evidence"]


def test_module_11_really_does_return_insufficient_for_every_candidate(
    connection, universe, lineage
):
    """The premise of the two tests above, verified against Module 11
    rather than assumed. If Module 17 ever populates cases and this stops
    holding, those tests should be revisited, not patched."""
    _features, _states, results = _run(connection, universe, lineage)

    for result in results:
        component = result.components["historical_evidence"]
        assert component.value is None
        assert SampleSufficiency.INSUFFICIENT.value in component.note


def test_the_components_that_did_measure_carry_real_spread(connection, universe, lineage):
    """A guard against the whole thing quietly computing constants.

    The twelve candidates differ in volume and in the phase of their
    wobble, so their liquidity and volatility components must differ too.
    """
    _features, _states, results = _run(connection, universe, lineage)

    liquidity = {result.components["volume_liquidity"].value for result in results}
    volatility = {result.components["volatility_structure"].value for result in results}

    assert len(liquidity) > 1
    assert len(volatility) > 1


def test_the_same_run_twice_produces_identical_results(connection, universe, lineage):
    """Reproducibility across the whole chain, not just the arithmetic."""
    _f1, _s1, first = _run(connection, universe, lineage)
    _f2, _s2, second = _run(connection, universe, lineage)

    assert [r.as_dict() for r in first] == [r.as_dict() for r in second]


def test_an_earlier_as_of_sees_less_and_says_so(connection, universe, lineage):
    """PIT correctness survives the whole chain: scoring a date before the
    bars existed produces refusals, not scores from later data."""
    benchmark = universe["SPY"]
    candidates = [sid for ticker, sid in universe.items() if ticker != "SPY"]
    early = START - timedelta(days=30)

    features = compute_features_batch(connection, candidates, early, market_security_id=benchmark)

    assert features.vectors == {}
    assert set(features.missing_securities) == set(candidates)
