"""The whole module, end to end, against real Module 08 output.

Universe -> feature vectors -> candidate pool -> six gates -> stored rows.

The unit tests build feature vectors by hand so a gate failure is
unambiguous. These deliberately do not: they run Module 08 over real bars
in a real database, because the seam between the two modules is itself
breakable in ways a hand-built fixture cannot see — a renamed feature, an
evidence field that stops being populated, a `MissReason` that quietly
stops propagating.

Three of the prompt's required claims are checked here rather than in the
unit tests, because all three are about that seam:

- a `MissReason`-flagged candidate routes to `INSUFFICIENT_EVIDENCE`
  rather than being scored on incomplete data;
- a thinly-traded small-cap survives the liquidity gate when its dollar
  volume is computed by Module 08 rather than asserted by me;
- the gates are batched, not looped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Connection

from core.candidate_detection import (
    DetectionConfig,
    detect_candidates,
    evaluate_eligibility,
    new_run_id,
    publish_detection_configuration,
    write_eligibility_results,
)
from core.candidate_detection.config import DetectionParameters, EligibilityParameters
from core.candidate_detection.eligibility.analogues import AnalogueCount
from core.candidate_detection.persistence import write_eligibility_results as write_results
from core.feature_engine.engine import compute_features_batch
from infra.db.enums import EligibilityGate, EvidenceStatus, ListingStatus
from infra.db.schema.intelligence import eligibility_check_results
from tests.integration.candidate_detection.conftest import (
    decline_then_base,
    insert_bars,
    rising,
)

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)

#: Comfortably past the 252-bar structural window.
SEASONED_BARS = 700

#: Chosen so the bars actually run up to `as_of`. `load_panel` bounds
#: history to the longest lookback window, so a series that ends months
#: early leaves that window only part-filled and the long-window features
#: come back None — which looks like a Module 09 bug and is a fixture bug.
SEASONED_START = datetime(2021, 9, 27)


@pytest.fixture
def universe(connection: Connection, register, add_member) -> dict[str, UUID]:
    """A small universe with a known correct verdict for each member.

    Volumes are chosen so `avg_dollar_volume` — which Module 08 computes
    from raw close times volume — lands where the liquidity gate needs it
    to for the test to mean anything.
    """
    ids: dict[str, UUID] = {}

    # A liquid base. ~12 * 500,000 = $6M/day.
    ids["liquid_base"] = register("BASE")
    insert_bars(
        connection,
        ids["liquid_base"],
        start=SEASONED_START,
        closes=decline_then_base(SEASONED_BARS),
        volume=500_000,
    )

    # The MLSS/SLS/QBTS profile: a real base at ~12 * 8,000 = $96,000/day.
    # Genuinely thin, and genuinely tradeable at retail size.
    ids["thin_base"] = register("THIN")
    insert_bars(
        connection,
        ids["thin_base"],
        start=SEASONED_START,
        closes=decline_then_base(SEASONED_BARS),
        volume=8_000,
    )

    # ~12 * 200 = $2,400/day. A position would be the tape.
    ids["untradeable"] = register("NOBID")
    insert_bars(
        connection,
        ids["untradeable"],
        start=SEASONED_START,
        closes=decline_then_base(SEASONED_BARS),
        volume=200,
    )

    # Recently listed: 40 bars, base-like on what little it has.
    ids["recent"] = register("NEWIPO")
    insert_bars(
        connection,
        ids["recent"],
        start=datetime(2024, 4, 8),
        closes=[8.0 + (index % 5) * 0.02 for index in range(40)],
        volume=300_000,
    )

    # At its highs — excluded at detection.
    ids["trending"] = register("TREND")
    insert_bars(
        connection,
        ids["trending"],
        start=SEASONED_START,
        closes=rising(SEASONED_BARS),
        volume=400_000,
    )

    for security_id in ids.values():
        add_member(security_id)
    return ids


@pytest.fixture
def features(connection: Connection, universe: dict[str, UUID]):
    return compute_features_batch(connection, list(universe.values()), AS_OF)


#: Detection keeps everything here on purpose. The selection cut is a
#: compute budget and is tested against a 400-security universe in
#: `tests/unit/candidate_detection/test_detection.py`; in a five-security
#: fixture it would keep exactly one name and every gate below would be
#: testing the pre-filter instead of the gate.
ADMIT_EVERYTHING = DetectionConfig(detection=DetectionParameters(selection_fraction=1.0))


@pytest.fixture
def pool(features):
    return detect_candidates(features, config=ADMIT_EVERYTHING, run_id=new_run_id())


class PlentifulAnalogues:
    """Stands in for Module 11 in the tests that are not about analogues.

    The provisional peer-profile counter counts cross-sectional peers, so
    in a five-security fixture it finds two and the gate fails everything —
    correctly, and uninformatively. That is a real property of the
    placeholder rather than a bug, and it is exercised on a 400-security
    universe in `tests/unit/candidate_detection/test_analogue_swap.py`.

    Injecting a stub here isolates the other five gates, and doubles as
    proof that the Module 11 seam works through the full pipeline rather
    than only in a unit test.
    """

    def count_analogues(self, securities, as_of, features):
        return {
            security_id: AnalogueCount(count=250, method="stub_historical", provisional=False)
            for security_id in securities
        }


# --------------------------------------------------------------------------
# The seam: real features, real gates
# --------------------------------------------------------------------------


def test_module_08_features_flow_into_detection_unchanged(features, universe):
    """The ranking components exist under the names detection expects.

    Guards the most boring and most likely breakage: a feature rename in
    Module 08 that leaves this module silently ranking on NaN.
    """
    from core.candidate_detection.detection import RANKING_COMPONENTS

    vector = features.vectors[universe["liquid_base"]]
    for name in RANKING_COMPONENTS:
        assert name in vector.features
        assert vector.features[name] is not None, f"{name} should compute for a seasoned security"


def test_a_security_at_its_highs_never_reaches_the_gates(pool, universe):
    from core.candidate_detection.pool import ExclusionReason

    assert universe["trending"] not in pool
    assert pool.excluded[universe["trending"]] is ExclusionReason.NOT_OFF_ITS_PEAK


# --------------------------------------------------------------------------
# LIQUIDITY, on Module 08's own numbers
# --------------------------------------------------------------------------


def test_a_thinly_traded_small_cap_survives_the_whole_pipeline(
    connection: Connection, pool, features, universe, universe_version_id: UUID
):
    """The claim that protects ARGUS's purpose, end to end.

    ~$96,000 of daily dollar volume, computed by Module 08 from real bars
    rather than asserted by the fixture. It reaches the pool and clears
    the liquidity gate. If this ever fails, ARGUS has quietly stopped
    being able to find the names it was built for.
    """
    assert universe["thin_base"] in pool

    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )
    outcome = report.outcomes[universe["thin_base"]]
    liquidity = outcome.results[EligibilityGate.LIQUIDITY]

    assert liquidity.passed, liquidity.detail
    assert liquidity.detail["avg_dollar_volume"] < 200_000, (
        "fixture sanity: this security must actually be thinly traded"
    )


def test_the_untradeable_security_is_excluded_on_liquidity_alone(
    connection: Connection, pool, features, universe, universe_version_id: UUID
):
    """~$2,400/day fails, and fails for that reason specifically.

    Asserting *which* gate failed matters: a security rejected by the
    right gate for the wrong reason would look identical in any summary
    that only counted rejections.
    """
    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )
    outcome = report.outcomes[universe["untradeable"]]

    assert not outcome.eligible
    assert EligibilityGate.LIQUIDITY in outcome.failed_gates
    assert EligibilityGate.BANKRUPTCY_RISK not in outcome.failed_gates


# --------------------------------------------------------------------------
# MissReason routing — the required claim
# --------------------------------------------------------------------------


def test_a_candidate_with_missing_inputs_routes_to_insufficient_evidence(
    connection: Connection, pool, features, universe, universe_version_id: UUID
):
    """Not scored on incomplete data — not evaluated at all, and said so.

    The recently-listed security ranks into the pool on the features it
    has. The gates then have to catch it, because Module 08 told them
    honestly how little was behind those numbers.
    """
    recent = universe["recent"]
    assert recent in pool, "must reach the gates, or there is nothing to test"

    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )
    outcome = report.outcomes[recent]

    assert outcome.status is EvidenceStatus.INSUFFICIENT_EVIDENCE
    assert EligibilityGate.DATA_HISTORY in outcome.failed_gates


def test_the_rejection_names_what_was_missing(
    connection: Connection, pool, features, universe, universe_version_id: UUID
):
    """An INSUFFICIENT_EVIDENCE that cannot say why is a shrug, not a diagnosis."""
    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )
    detail = report.outcomes[universe["recent"]].results[EligibilityGate.DATA_HISTORY].detail

    assert detail["bars_available"] < detail["bars_required"]
    assert 0 < detail["coverage_ratio"] < 1


def test_miss_reasons_from_module_08_reach_the_gate_detail(
    connection: Connection, pool, features, universe, universe_version_id: UUID
):
    """The taxonomy survives the whole chain, not just the first hop.

    Module 07 defines `MissReason`; Module 08 attaches it to every vector;
    this asserts it is still legible after Module 09 has turned it into a
    gate verdict. Each hop is a place it could be dropped.
    """
    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )
    detail = report.outcomes[universe["liquid_base"]].results[EligibilityGate.DATA_QUALITY].detail

    assert detail["missing_inputs"]["sector_benchmark"] == "never_ingested"


def test_a_fully_supported_candidate_is_eligible(
    connection: Connection, pool, features, universe, universe_version_id: UUID
):
    """The control. Without it, a runner that failed everything would pass."""
    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )
    outcome = report.outcomes[universe["liquid_base"]]

    assert outcome.eligible, outcome.reason_summary()
    assert outcome.status is EvidenceStatus.SCORED


# --------------------------------------------------------------------------
# VALID_ASSET_IDENTITY, through Module 06's real machinery
# --------------------------------------------------------------------------


def test_a_delisted_security_fails_the_identity_gate(
    connection: Connection, register, add_member, universe_version_id: UUID
):
    """Retained in the universe, excluded as a live candidate."""
    security_id = register("GONE")
    insert_bars(
        connection,
        security_id,
        start=SEASONED_START,
        closes=decline_then_base(SEASONED_BARS),
        volume=400_000,
    )
    add_member(security_id, status=ListingStatus.DELISTED)

    features = compute_features_batch(connection, [security_id], AS_OF)
    pool = detect_candidates(features, config=ADMIT_EVERYTHING, run_id=new_run_id())
    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )

    outcome = report.outcomes[security_id]
    assert EligibilityGate.VALID_ASSET_IDENTITY in outcome.failed_gates


def test_a_security_absent_from_the_universe_version_fails_identity(
    connection: Connection, register, universe_version_id: UUID
):
    """No membership row covering `as_of` is a failure, not a pass."""
    security_id = register("UNKNOWN")
    insert_bars(
        connection,
        security_id,
        start=SEASONED_START,
        closes=decline_then_base(SEASONED_BARS),
        volume=400_000,
    )
    # Deliberately not added to the universe version.

    features = compute_features_batch(connection, [security_id], AS_OF)
    pool = detect_candidates(features, config=ADMIT_EVERYTHING, run_id=new_run_id())
    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )

    result = report.outcomes[security_id].results[EligibilityGate.VALID_ASSET_IDENTITY]
    assert not result.passed
    assert result.detail["resolved"] is False


# --------------------------------------------------------------------------
# Every gate runs, and every result is recorded
# --------------------------------------------------------------------------


def test_all_six_gates_are_evaluated_for_every_candidate(
    connection: Connection, pool, features, universe_version_id: UUID
):
    """No short-circuiting. Six verdicts per candidate, always."""
    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )

    for outcome in report.outcomes.values():
        assert set(outcome.results) == set(EligibilityGate)


def test_a_candidate_can_fail_several_gates_at_once(
    connection: Connection, pool, features, universe, universe_version_id: UUID
):
    """ "Failed liquidity" and "failed liquidity and history" are different facts.

    Short-circuiting would report only the first and make the second
    unknowable — which is the difference between "untradeable today" and
    "not a real candidate at all".
    """
    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )
    counts = report.failures_by_gate()

    assert counts[EligibilityGate.LIQUIDITY] >= 1
    assert counts[EligibilityGate.DATA_HISTORY] >= 1


def test_gate_results_are_persisted_including_passes(
    connection: Connection, pool, features, universe_version_id: UUID
):
    """Passes are stored too, or "evaluated and passed" becomes "never run"."""
    config_id = publish_detection_configuration(connection, DetectionConfig())
    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )
    report = type(report)(
        as_of=report.as_of,
        run_id=report.run_id,
        detection_configuration_id=config_id,
        outcomes=report.outcomes,
    )

    written = write_eligibility_results(connection, report)
    assert written == len(report.outcomes) * len(EligibilityGate)

    stored = (
        connection.execute(
            select(eligibility_check_results.c.passed).where(
                eligibility_check_results.c.run_id == report.run_id
            )
        )
        .scalars()
        .all()
    )
    assert any(stored) and not all(stored), "both passes and failures must be recorded"


def test_rewriting_the_same_report_inserts_nothing(
    connection: Connection, pool, features, universe_version_id: UUID
):
    """Insert-only: a recorded verdict is what a rejection cited."""
    config_id = publish_detection_configuration(connection, DetectionConfig())
    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )
    stamped = type(report)(
        as_of=report.as_of,
        run_id=report.run_id,
        detection_configuration_id=config_id,
        outcomes=report.outcomes,
    )

    assert write_results(connection, stamped) > 0
    assert write_results(connection, stamped) == 0


def test_an_unstamped_report_is_refused(
    connection: Connection, pool, features, universe_version_id: UUID
):
    """No configuration ID, no write — the same rule as Module 08's vectors."""
    report = evaluate_eligibility(
        connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
    )
    assert report.detection_configuration_id is None

    with pytest.raises(ValueError, match="detection_configuration_id"):
        write_eligibility_results(connection, report)


def test_the_provisional_analogue_gate_still_fires_on_real_data(
    connection: Connection, pool, features, universe_version_id: UUID
):
    """The default counter is not bypassed everywhere — it is exercised here.

    Without this, `PlentifulAnalogues` would mean the shipped counter never
    runs through the real pipeline at all, and a break in it would show up
    only after Module 11 was already assumed to be the replacement.

    Five securities give almost no peers, so the gate rejects. That is the
    placeholder's documented weakness rather than a defect, and pinning it
    here makes the weakness visible instead of hidden behind a stub.
    """
    report = evaluate_eligibility(connection, pool, features, universe_version_id)

    for outcome in report.outcomes.values():
        result = outcome.results[EligibilityGate.MINIMUM_HISTORICAL_ANALOGUES]
        assert result.detail["method"] == "peer_profile"
        assert result.detail["provisional"] is True


def test_the_configuration_is_idempotent_by_checksum(connection: Connection):
    """Publishing the same config twice yields one row, as Module 08 does.

    Without it, every scan would mint a new configuration ID for identical
    gate parameters and "which thresholds rejected this candidate" would
    stop being answerable within a week of running.
    """
    first = publish_detection_configuration(connection, DetectionConfig())
    second = publish_detection_configuration(connection, DetectionConfig())
    assert first == second


def test_a_changed_configuration_becomes_a_new_version(connection: Connection):
    """Append-only: the old thresholds survive to explain old rejections."""
    baseline = publish_detection_configuration(connection, DetectionConfig())
    stricter = publish_detection_configuration(
        connection,
        DetectionConfig(eligibility=EligibilityParameters(min_avg_dollar_volume=1_000_000.0)),
    )
    assert baseline != stricter


# --------------------------------------------------------------------------
# Batched, not looped
# --------------------------------------------------------------------------


class QueryCounter:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self.count = 0

    def _on_execute(self, *_a: object, **_k: object) -> None:
        self.count += 1

    def __enter__(self) -> QueryCounter:
        event.listen(self._connection, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self._connection, "before_cursor_execute", self._on_execute)


def test_eligibility_issues_a_constant_number_of_queries(
    connection: Connection, pool, features, universe_version_id: UUID
):
    """Five queries for the batch: one universe listing, four fundamentals.

    The same property Module 08's suite asserts, for the same reason: at
    five queries *per security*, a full-universe scan across fifteen years
    of dates is unrunnable rather than merely slow. A disguised loop
    cannot fake a constant query count.
    """
    with QueryCounter(connection) as counter:
        evaluate_eligibility(
            connection, pool, features, universe_version_id, analogue_counter=PlentifulAnalogues()
        )

    assert counter.count == 5, f"expected 5 queries, got {counter.count}"


def test_the_query_count_does_not_grow_with_the_candidate_count(
    connection: Connection, features, universe_version_id: UUID
):
    """Constant, not merely small — the claim is about scaling.

    Asserting `== 5` for one pool size would also pass an implementation
    that batched in chunks; comparing across sizes is what makes it a
    statement about growth.
    """
    everything = detect_candidates(features, config=ADMIT_EVERYTHING, run_id=new_run_id())
    one = detect_candidates(
        features,
        config=DetectionConfig(detection=DetectionParameters(selection_fraction=0.001)),
        run_id=new_run_id(),
    )
    assert len(one) < len(everything)

    with QueryCounter(connection) as few:
        evaluate_eligibility(
            connection, one, features, universe_version_id, analogue_counter=PlentifulAnalogues()
        )
    with QueryCounter(connection) as many:
        evaluate_eligibility(
            connection,
            everything,
            features,
            universe_version_id,
            analogue_counter=PlentifulAnalogues(),
        )

    assert few.count == many.count == 5
