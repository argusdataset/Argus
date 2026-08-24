"""Would this design survive 10,000 securities x 15 years?

The tests run at fixture scale — five or ten securities over three months
— and that is the point of them. They do not measure whether the engine is
fast; they assert the two structural properties that decide whether it
*can* be, so that a change which quietly reintroduces a per-security round
trip fails here rather than in a run that has already burned a day.

The properties, in the spirit of Modules 08 and 09's own vectorization
tests:

1. **The panel load is once per scan date, not once per security.** Module
   08 built a batch path whose central claim is "two queries regardless of
   universe size"; a replay that called the single-security path in a loop
   would work perfectly on five names and issue 150,000 queries per scan
   date on a real universe.
2. **The historical case set is loaded once per scan date, not once per
   candidate.** Module 11 accepts `cases=` precisely so a batch caller can
   do this. Over a real run the difference is thousands of queries against
   millions.

And one claim about the configuration rather than the code: `batch_size`
is tagged `operational`, meaning it bounds memory and provably cannot
change a result. That claim is tested, not asserted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection

from core.model_validation_evaluation.validation.config import (
    ReplaySettings,
    ValidationConfig,
    ValidationSetting,
)
from core.model_validation_evaluation.validation.replay import (
    ModuleConfigs,
    ReplayRequest,
    compute_features_in_chunks,
    replay,
)
from core.scoring.engine import Lineage
from infra.db.schema.identity import universe_membership
from tests.integration.feature_engine.conftest import insert_bars
from tests.integration.model_validation_evaluation.conftest import PERIOD_START

HISTORY_START = datetime(2019, 1, 2, tzinfo=UTC)
#: One week, so a replay covers exactly one scan date and the query counts
#: below are per-scan-date rather than per-period.
ONE_SCAN_END = PERIOD_START + timedelta(days=3)


def _closes(index: int, length: int) -> list[float]:
    return [
        100.0 - 55.0 * min(n / (length * 0.4), 1.0) + index + ((n % 5) - 2) * 0.3
        for n in range(length)
    ]


@pytest.fixture
def seed_universe(connection: Connection, register, lineage: Lineage):
    """Add `count` securities with full price history to the universe."""
    import pandas as pd

    length = len(pd.bdate_range(HISTORY_START, ONE_SCAN_END))

    def _seed(count: int, prefix: str) -> list[UUID]:
        ids = []
        for index in range(count):
            security_id = register(f"{prefix}{index}")
            insert_bars(connection, security_id, start=HISTORY_START, closes=_closes(index, length))
            connection.execute(
                universe_membership.insert().values(
                    universe_version_id=lineage.universe_version_id,
                    security_id=security_id,
                    listing_status="LISTED",
                    listed_from=HISTORY_START,
                    listed_to=None,
                    exchange="NASDAQ",
                    interval_evidence="reported",
                )
            )
            ids.append(security_id)
        return ids

    return _seed


def _request(lineage: Lineage, security_ids: list[UUID] | None = None) -> ReplayRequest:
    return ReplayRequest(
        period_start=PERIOD_START,
        period_end=ONE_SCAN_END,
        lineage=lineage,
        security_ids=security_ids,
    )


def _matching(statements: list[str], needle: str) -> list[str]:
    return [s for s in statements if needle in s.lower()]


def test_the_price_panel_is_loaded_once_per_scan_date_not_once_per_security(
    connection: Connection,
    lineage: Lineage,
    modules: ModuleConfigs,
    seed_universe,
    count_queries,
):
    """The property that decides whether a real run is hours or weeks."""
    small = seed_universe(2, "SMALL")
    with count_queries(connection) as statements:
        replay(connection, _request(lineage, small), modules=modules)
    small_loads = len(_matching(statements, "canonical_ohlcv"))

    large = seed_universe(8, "LARGE")
    with count_queries(connection) as statements:
        replay(connection, _request(lineage, small + large), modules=modules)
    large_loads = len(_matching(statements, "canonical_ohlcv"))

    assert small_loads == large_loads, (
        f"{small_loads} bar queries for 2 securities and {large_loads} for 10 — the "
        "panel load has become per-security, which at 10,000 names is fatal."
    )


def test_the_historical_case_set_is_loaded_once_per_scan_date(
    connection: Connection,
    lineage: Lineage,
    modules: ModuleConfigs,
    seed_universe,
    count_queries,
):
    """Module 11 accepts `cases=` for exactly this. Reloading per candidate
    would be one query per candidate per date — millions over a real run."""
    securities = seed_universe(6, "CASELOAD")

    with count_queries(connection) as statements:
        result = replay(connection, _request(lineage, securities), modules=modules)

    case_loads = [
        s for s in statements if "setup_outcomes" in s.lower() and "feature_vectors" in s.lower()
    ]

    assert len(result.scan_dates) == 1
    assert len(case_loads) <= 1, (
        f"{len(case_loads)} case loads for one scan date. The case set must be loaded "
        "once and passed to every candidate."
    )


def test_doubling_the_universe_does_not_double_the_query_count(
    connection: Connection,
    lineage: Lineage,
    modules: ModuleConfigs,
    seed_universe,
    count_queries,
):
    """The whole-scan version of the claim.

    Some growth is expected and correct — Modules 11 and 12 are per
    candidate by their own deliberate design, and the candidate pool grows
    with the universe. What must not happen is proportional growth in the
    universe-level work, which is what a loop-shaped rewrite would produce.
    """
    small = seed_universe(3, "GROWA")
    with count_queries(connection) as statements:
        replay(connection, _request(lineage, small), modules=modules)
    small_total = len(statements)

    large = seed_universe(9, "GROWB")
    with count_queries(connection) as statements:
        replay(connection, _request(lineage, small + large), modules=modules)
    large_total = len(statements)

    assert large_total < small_total * 2, (
        f"quadrupling the universe took {large_total} queries against {small_total} — "
        "the per-scan-date cost is scaling with universe size."
    )


def test_chunking_the_feature_pass_cannot_change_the_result(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, seed_universe
):
    """`batch_size` is tagged `operational`, and this is what that claims.

    A constant that fails this test is not operational; it is a
    calibratable threshold that was mislabelled, and the config entry
    would be a lie.
    """
    securities = seed_universe(6, "CHUNK")

    one_pass = compute_features_in_chunks(
        connection,
        securities,
        as_of=ONE_SCAN_END,
        spec=modules.features,
        feature_schema_version_id=lineage.feature_schema_version_id,
        chunk_size=len(securities) * 2,
    )
    many_passes = compute_features_in_chunks(
        connection,
        securities,
        as_of=ONE_SCAN_END,
        spec=modules.features,
        feature_schema_version_id=lineage.feature_schema_version_id,
        chunk_size=1,
    )

    assert set(one_pass.vectors) == set(many_passes.vectors)
    for security_id in one_pass.vectors:
        assert (
            one_pass.vectors[security_id].features == many_passes.vectors[security_id].features
        ), f"chunking changed {security_id}'s features"


def test_a_chunked_replay_produces_the_same_scan_result_as_an_unchunked_one(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, seed_universe
):
    """The end-to-end version, including the cross-sectional ranking.

    This is the assertion that would fail if detection were ever chunked
    alongside the features: ranking each chunk against itself would select
    the top slice of every chunk, which is a different candidate pool.
    """
    securities = seed_universe(8, "RANKCHUNK")

    def _run(chunk: float) -> dict:
        config = ValidationConfig(
            settings=ReplaySettings(
                batch_size=ValidationSetting(value=chunk, kind="operational", rationale="test")
            )
        )
        result = replay(connection, _request(lineage, securities), modules=modules, config=config)
        scan = result.scan_dates[0]
        return {
            "candidates": scan.candidates,
            "features": scan.features_computed,
            "states": scan.state_distribution,
        }

    assert _run(100.0) == _run(2.0)


def test_the_chunk_size_actually_chunks(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, seed_universe, count_queries
):
    """The equivalence test above would also pass if chunking did nothing.

    So: a chunk size of one must issue more panel loads than a chunk size
    covering the whole universe. Without this, `batch_size` could be
    silently ignored and every test here would still be green.
    """
    securities = seed_universe(6, "REALCHUNK")

    with count_queries(connection) as statements:
        compute_features_in_chunks(
            connection,
            securities,
            as_of=ONE_SCAN_END,
            spec=modules.features,
            feature_schema_version_id=lineage.feature_schema_version_id,
            chunk_size=len(securities) * 2,
        )
    unchunked = len(_matching(statements, "canonical_ohlcv"))

    with count_queries(connection) as statements:
        compute_features_in_chunks(
            connection,
            securities,
            as_of=ONE_SCAN_END,
            spec=modules.features,
            feature_schema_version_id=lineage.feature_schema_version_id,
            chunk_size=1,
        )
    chunked = len(_matching(statements, "canonical_ohlcv"))

    assert chunked > unchunked
    assert chunked == unchunked * len(securities)


def test_an_empty_universe_costs_nothing_and_returns_an_empty_result(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs
):
    """A scan date with nobody listed must not attempt a panel load."""
    result = compute_features_in_chunks(
        connection,
        [],
        as_of=ONE_SCAN_END,
        spec=modules.features,
        feature_schema_version_id=lineage.feature_schema_version_id,
        chunk_size=100,
    )

    assert result.vectors == {}
    assert result.feature_schema_version_id == lineage.feature_schema_version_id
