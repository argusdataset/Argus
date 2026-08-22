"""Missing evidence has to survive the transformation into a feature.

Module 07's promise is that a missing input is an explicit, checkable
state — never a bare `None`, never a silent fallback. A feature engine is
where that promise is easiest to break, because it *transforms*: dozens
of inputs go in and one tidy vector comes out, and "twelve of the sixty
bars were missing" has nowhere obvious to go.

The failure mode is specific and quiet. A 60-bar mean over 12 bars of
history, with the other 48 zero-filled, is a number. It has the right
dtype, it sorts, it ranks, and it is meaningless. Module 09's
`INSUFFICIENT_EVIDENCE` gate exists precisely so a candidate is never
scored on that number — and the gate can only fire if this module hands
it the truth.

So: a recently-listed security must produce a vector that *says* it is
thin, an absent security must be reported rather than dropped, and an
unavailable feature must be `None` rather than 0.0.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import Connection

from core.data_validation.result import MissReason
from core.feature_engine.engine import compute_features, compute_features_batch
from core.feature_engine.spec import FEATURE_NAMES, FeatureSpec
from tests.integration.feature_engine.conftest import insert_bars

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)

#: Long enough to compute the short/medium windows, far short of the
#: 252-bar structural one. A genuinely awkward middle case: some features
#: are real, some cannot exist, and the vector has to distinguish them.
RECENT_LISTING_BARS = 30


@pytest.fixture
def recently_listed(connection: Connection, register) -> UUID:
    """A security with 30 sessions of history and nothing before that."""
    security_id = register("NEWCO")
    insert_bars(
        connection,
        security_id,
        start=datetime(2024, 4, 22),
        closes=[10.0 + index * 0.1 for index in range(RECENT_LISTING_BARS)],
    )
    return security_id


@pytest.fixture
def fully_seasoned(connection: Connection, register) -> UUID:
    """A security with four years of history — the control."""
    security_id = register("OLDCO")
    insert_bars(
        connection,
        security_id,
        start=datetime(2020, 6, 1),
        closes=[10.0 + (index % 40) * 0.1 for index in range(1000)],
    )
    return security_id


# --------------------------------------------------------------------------
# Insufficient history is reported, not papered over
# --------------------------------------------------------------------------


def test_a_thin_security_still_produces_a_vector(connection: Connection, recently_listed: UUID):
    """Thin evidence is not an error. It is a fact about the security.

    Raising, or returning nothing, would make a recently-listed security
    invisible to the scan rather than visibly unqualified — and ARGUS
    keeps failed and ineligible setups, it does not delete them.
    """
    vector = compute_features(connection, recently_listed, AS_OF)
    assert vector.security_id == recently_listed
    assert set(vector.features) == set(FEATURE_NAMES)


def test_the_evidence_says_the_history_is_insufficient(
    connection: Connection, recently_listed: UUID
):
    """The claim Module 09's INSUFFICIENT_EVIDENCE gate will read."""
    evidence = compute_features(connection, recently_listed, AS_OF).evidence

    assert evidence.bars_available == RECENT_LISTING_BARS
    assert evidence.bars_required == FeatureSpec().windows.max_lookback
    assert not evidence.has_sufficient_history
    assert not evidence.is_complete
    assert 0.0 < evidence.coverage_ratio < 1.0


def test_long_window_features_are_none_and_never_zero(
    connection: Connection, recently_listed: UUID
):
    """The distinction the whole module rests on.

    0.0 is a measurement: "this security's drawdown from its 252-bar peak
    is nil". None is the absence of one. A downstream ranking that saw
    0.0 here would place a 30-day-old listing at the top of a
    "shallowest drawdown" sort, which is precisely the plausible-looking
    wrong answer ARGUS exists to prevent.
    """
    vector = compute_features(connection, recently_listed, AS_OF)

    for name in ("peak_to_trough_decline", "drawdown_pct", "decline_duration_bars"):
        assert vector.features[name] is None, f"{name} cannot exist with 30 bars of history"
        assert name in vector.evidence.unavailable_features


def test_short_window_features_still_compute(connection: Connection, recently_listed: UUID):
    """Partial evidence is partial, not worthless.

    Discarding the whole vector because one window could not be filled
    would throw away real measurements — and would make the coverage
    ratio meaningless, since it would only ever be 1.0.
    """
    vector = compute_features(connection, recently_listed, AS_OF)
    computed = vector.available_features()

    assert computed, "30 bars is enough for the short and medium windows"
    assert "rvol" in computed
    assert len(computed) < len(FEATURE_NAMES)


def test_unavailable_features_are_listed_exhaustively(
    connection: Connection, recently_listed: UUID
):
    """`unavailable_features` and the None values must agree.

    Two representations of the same fact are two chances to disagree, so
    the test pins them together rather than trusting either alone.
    """
    vector = compute_features(connection, recently_listed, AS_OF)
    from_features = {name for name, value in vector.features.items() if value is None}
    assert from_features == set(vector.evidence.unavailable_features)


def test_a_seasoned_security_reports_sufficient_history(
    connection: Connection, fully_seasoned: UUID
):
    """The control. Without it, an engine that always says 'insufficient' passes."""
    evidence = compute_features(connection, fully_seasoned, AS_OF).evidence
    assert evidence.has_sufficient_history
    assert evidence.coverage_ratio == 1.0
    assert evidence.bars_available >= evidence.bars_required


# --------------------------------------------------------------------------
# A security with no data at all
# --------------------------------------------------------------------------


def test_a_security_with_no_bars_is_reported_not_dropped(connection: Connection, register):
    """Silently shrinking the result set is how a universe loses members.

    A caller that asked for 10,000 securities and received 9,300 vectors
    with no explanation cannot tell survivorship bias from a quiet bug.
    """
    known = register("HASDATA")
    insert_bars(connection, known, start=datetime(2024, 4, 22), closes=[10.0] * 30)
    silent = register("NODATA")

    result = compute_features_batch(connection, [known, silent], AS_OF)

    assert set(result.vectors) == {known}
    assert result.missing_securities == {silent: MissReason.NOT_YET_AVAILABLE}


def test_every_requested_security_appears_somewhere(connection: Connection, register):
    """Requested == returned + reported-missing. No security vanishes."""
    known = register("HASDATA")
    insert_bars(connection, known, start=datetime(2024, 4, 22), closes=[10.0] * 30)
    requested = [known, register("NODATA1"), register("NODATA2"), uuid4()]

    result = compute_features_batch(connection, requested, AS_OF)

    assert set(result.vectors) | set(result.missing_securities) == set(requested)


def test_the_single_security_path_reports_a_total_miss_too(connection: Connection, register):
    """`compute_features` must not hide behind the batch result's shape.

    It delegates to the batch path, so a security absent from
    `vectors` has to be turned back into something the caller can read —
    a vector of Nones with a `price_history` MissReason, not a KeyError.
    """
    vector = compute_features(connection, register("NOTHING"), AS_OF)

    assert vector.evidence.bars_available == 0
    assert vector.evidence.missing_inputs["price_history"] is MissReason.NOT_YET_AVAILABLE
    assert set(vector.evidence.unavailable_features) == set(FEATURE_NAMES)
    assert vector.available_features() == {}
    assert vector.event_time is None


def test_a_security_listed_after_the_as_of_date_has_no_history(connection: Connection, register):
    """PIT and evidence are the same mechanism seen from two sides.

    A security whose first bar postdates `as_of` is not "missing data" —
    it did not yet exist to ARGUS. Both facts arrive through the same
    `availability_time` filter, and the vector reports the second as the
    first, which is the honest reading.
    """
    security_id = register("FUTURE")
    insert_bars(connection, security_id, start=datetime(2025, 1, 2), closes=[10.0] * 30)

    result = compute_features_batch(connection, [security_id], AS_OF)
    assert result.missing_securities == {security_id: MissReason.NOT_YET_AVAILABLE}


# --------------------------------------------------------------------------
# Missing benchmarks
# --------------------------------------------------------------------------


def test_an_absent_sector_benchmark_is_named_as_a_missing_input(
    connection: Connection, fully_seasoned: UUID
):
    """ARGUS stores no sector data, so two features cannot be computed.

    The vector must say which input was absent, not merely leave two NaNs
    for a caller to guess about. This is the case that will still be true
    after Module 09 ships, so it needs to be legible now.
    """
    vector = compute_features(connection, fully_seasoned, AS_OF)

    assert vector.evidence.missing_inputs["sector_benchmark"] is MissReason.NEVER_INGESTED
    assert vector.evidence.missing_inputs["industry_benchmark"] is MissReason.NEVER_INGESTED
    assert vector.features["rs_vs_sector"] is None
    assert vector.features["rs_vs_industry"] is None


def test_supplying_a_market_benchmark_clears_that_missing_input(
    connection: Connection, fully_seasoned: UUID, register
):
    """The mirror: a benchmark that IS available must not be reported missing.

    Without this, `missing_inputs` could be a hardcoded constant and every
    test above would still pass.
    """
    market = register("SPY")
    insert_bars(
        connection,
        market,
        start=datetime(2020, 6, 1),
        closes=[100.0 + index * 0.05 for index in range(1000)],
    )

    vector = compute_features(connection, fully_seasoned, AS_OF, market_security_id=market)

    assert "market_benchmark" not in vector.evidence.missing_inputs
    assert vector.features["rs_vs_market"] is not None
    # And the sector benchmark is still honestly absent.
    assert vector.evidence.missing_inputs["sector_benchmark"] is MissReason.NEVER_INGESTED


def test_evidence_travels_with_every_vector_in_a_batch(connection: Connection, register):
    """Per-security evidence, not one summary for the whole run.

    A batch mixing seasoned and newly-listed securities must give each its
    own answer — a single batch-level flag would force Module 09 to
    discard the entire universe whenever one member was thin.
    """
    thin = register("THIN")
    insert_bars(connection, thin, start=datetime(2024, 4, 22), closes=[10.0] * 30)
    thick = register("THICK")
    insert_bars(connection, thick, start=datetime(2020, 6, 1), closes=[10.0] * 1000)

    result = compute_features_batch(connection, [thin, thick], AS_OF)

    assert not result.vectors[thin].evidence.has_sufficient_history
    assert result.vectors[thick].evidence.has_sufficient_history
    assert set(result.with_sufficient_history()) == {thick}
