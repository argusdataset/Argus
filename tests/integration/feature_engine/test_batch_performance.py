"""Is the batch path actually vectorized, or a loop wearing a coat?

The module brief asks for "batch-first, genuinely vectorized — not a
per-ticker loop in disguise". That is easy to claim and easy to fail
invisibly: a `compute_features_batch` that iterates `security_ids` and
calls the single-security path has the same signature, the same return
type, and the same numbers. Only the cost differs, and at ten securities
nobody notices.

Three properties are asserted here, in increasing order of how hard they
are to fake:

1. **Timing.** The weakest — real, but noisy on shared CI hardware, so
   the margin required is deliberately loose.
2. **Identical results.** Not a performance property at all, but the one
   that makes the other two worth having: if the batch and loop paths
   disagreed, the speed would be irrelevant.
3. **Query count.** The strongest. Two SQL round trips for the whole
   universe versus two per security. A disguised loop cannot fake this —
   whatever it does with numpy, it still has to ask the database N times.

Property 3 is why this file exists rather than a `pytest-benchmark`
fixture. Wall-clock time can be gamed by caching; the query count is
structural.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection

from core.feature_engine.engine import compute_features, compute_features_batch
from core.feature_engine.spec import FEATURE_NAMES
from tests.integration.feature_engine.conftest import insert_bars

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)

#: Enough securities that a per-security loop is measurably worse, few
#: enough that the fixture inserts in a few seconds. The property under
#: test is structural, so it does not need a realistic universe size to
#: hold — and a test that took a minute to set up would stop being run.
UNIVERSE = 25

#: Comfortably past the 252-bar structural window, so every feature
#: actually computes and the comparison is over real work.
BARS = 300


@pytest.fixture
def universe(connection: Connection, register) -> list[UUID]:
    """A small universe of seasoned securities with differing price paths."""
    ids: list[UUID] = []
    for index in range(UNIVERSE):
        security_id = register(f"BATCH{index:03d}")
        insert_bars(
            connection,
            security_id,
            start=datetime(2023, 1, 2),
            closes=[10.0 + index + (bar % 37) * 0.25 for bar in range(BARS)],
            volume=1_000_000 + index * 1_000,
        )
        ids.append(security_id)
    return ids


class QueryCounter:
    """Counts SQL statements issued on one connection."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self.count = 0

    def _on_execute(self, *_args: object, **_kwargs: object) -> None:
        self.count += 1

    def __enter__(self) -> QueryCounter:
        event.listen(self._connection, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self._connection, "before_cursor_execute", self._on_execute)


# --------------------------------------------------------------------------
# Property 3 — the structural one
# --------------------------------------------------------------------------


def test_the_batch_path_issues_two_queries_for_the_whole_universe(
    connection: Connection, universe: list[UUID]
):
    """One query for bars, one for corporate actions. Regardless of N.

    This is the property a disguised loop cannot fake. It is also the one
    that decides whether a 10,000-security historical replay is minutes or
    days: at two queries per security per date, a fifteen-year daily
    backtest is 75 million round trips.
    """
    with QueryCounter(connection) as counter:
        compute_features_batch(connection, universe, AS_OF)

    assert counter.count == 2, f"expected 2 queries for {len(universe)} securities"


def test_the_query_count_does_not_grow_with_the_universe(
    connection: Connection, universe: list[UUID]
):
    """Constant, not merely small.

    Asserting `== 2` for one universe size would also pass for an
    implementation that batched in chunks of 25. Comparing across sizes is
    what makes it a statement about scaling.
    """
    with QueryCounter(connection) as few:
        compute_features_batch(connection, universe[:5], AS_OF)
    with QueryCounter(connection) as many:
        compute_features_batch(connection, universe, AS_OF)

    assert few.count == many.count == 2


def test_the_loop_path_issues_two_queries_per_security(
    connection: Connection, universe: list[UUID]
):
    """The baseline, measured rather than assumed.

    Without this, `== 2` above proves nothing: it could be that the
    engine only ever issues two queries no matter how it is called, and
    the batch entry point is doing nothing special.
    """
    subset = universe[:5]
    with QueryCounter(connection) as counter:
        for security_id in subset:
            compute_features(connection, security_id, AS_OF)

    assert counter.count == 2 * len(subset)


# --------------------------------------------------------------------------
# Property 2 — the results must agree
# --------------------------------------------------------------------------


def test_batch_and_loop_produce_identical_features(connection: Connection, universe: list[UUID]):
    """One implementation, two entry points — asserted, not assumed.

    `compute_features` delegates to `compute_features_batch`, so this
    should hold by construction. Testing it anyway guards the direction of
    that delegation: if someone ever "optimizes" the single-security path
    with its own arithmetic, the two would drift and every debugging
    session done through `compute_features` would be misleading.
    """
    subset = universe[:5]
    batched = compute_features_batch(connection, subset, AS_OF)

    for security_id in subset:
        looped = compute_features(connection, security_id, AS_OF)
        from_batch = batched.vectors[security_id]

        assert looped.features == pytest.approx(from_batch.features, nan_ok=True)
        assert looped.event_time == from_batch.event_time
        assert looped.availability_time == from_batch.availability_time
        assert looped.evidence.bars_available == from_batch.evidence.bars_available


def test_batch_shape_is_one_complete_vector_per_security(
    connection: Connection, universe: list[UUID]
):
    """Shape, stated plainly: N securities in, N fully-keyed vectors out."""
    result = compute_features_batch(connection, universe, AS_OF)

    assert len(result.vectors) == UNIVERSE
    assert not result.missing_securities
    for vector in result:
        assert set(vector.features) == set(FEATURE_NAMES)
        assert vector.as_of == AS_OF


def test_a_duplicated_security_is_computed_once(connection: Connection, universe: list[UUID]):
    """Callers pass unclean lists. The engine must not pay twice for them."""
    duplicated = [universe[0], universe[0], universe[1]]
    result = compute_features_batch(connection, duplicated, AS_OF)
    assert len(result.vectors) == 2


# --------------------------------------------------------------------------
# Property 1 — the weakest, and honest about it
# --------------------------------------------------------------------------


def test_the_batch_path_is_meaningfully_faster_than_the_loop(
    connection: Connection, universe: list[UUID]
):
    """Wall-clock, with a deliberately loose margin.

    The requirement is "meaningfully faster", and the margin here is 3x
    rather than the ~12x the query counts imply, because this runs on
    whatever hardware CI provides and a flaky performance test gets
    disabled, which is worse than a loose one. The tight, reliable version
    of this claim is the query-count test above; this exists to confirm
    the structural advantage shows up as real time.
    """
    loop_started = time.perf_counter()
    for security_id in universe:
        compute_features(connection, security_id, AS_OF)
    loop_seconds = time.perf_counter() - loop_started

    batch_started = time.perf_counter()
    compute_features_batch(connection, universe, AS_OF)
    batch_seconds = time.perf_counter() - batch_started

    assert batch_seconds * 3 < loop_seconds, (
        f"batch {batch_seconds:.3f}s vs loop {loop_seconds:.3f}s "
        f"for {UNIVERSE} securities — the batch path is not pulling its weight"
    )


def test_the_per_security_cost_falls_as_the_universe_grows(
    connection: Connection, universe: list[UUID]
):
    """Marginal cost per security falls as the panel gets wider.

    Worth being precise about what vectorization does and does not buy,
    because the obvious version of this test asserts the wrong thing. The
    numerical work is genuinely O(bars x securities): a 20-bar mean over
    25 columns really is five times the arithmetic of one over 5 columns,
    and no amount of vectorizing changes that. What the wide-frame layout
    removes is the *fixed* cost paid per security — two query round trips,
    a pandas call's dispatch overhead, a `FeatureVector` construction —
    by amortizing it across the whole universe.

    So the measurable claim is amortization, not sublinear arithmetic:
    cost per security must *fall* as the universe grows. Measured on this
    fixture it drops by roughly a third between 5 and 25 securities; the
    assertion asks only that it falls at all, since the margin is
    hardware-dependent and the structural claim is the query count above.
    """

    def seconds_per_security(securities: list[UUID]) -> float:
        compute_features_batch(connection, securities, AS_OF)  # warm caches
        started = time.perf_counter()
        compute_features_batch(connection, securities, AS_OF)
        return (time.perf_counter() - started) / len(securities)

    small = seconds_per_security(universe[:5])
    large = seconds_per_security(universe)

    assert large < small, (
        f"{small * 1000:.1f}ms per security at 5, {large * 1000:.1f}ms at {UNIVERSE} — "
        "the fixed cost is not being amortized across the universe"
    )
