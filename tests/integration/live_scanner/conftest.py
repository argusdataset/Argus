"""Database fixtures for Module 18.

Almost everything this module does is about durability and transactions —
a row that survives a crash, a retry that gets a fresh transaction, a
scan that is not re-run because a stored row says it already happened. A
stubbed connection would prove none of it.

## The connection factory

`run_scan` takes something that *opens* a connection, because a retry
needs a fresh transaction — the one that just died is aborted, and every
statement on it would raise. Production passes `engine.begin`; these tests
pass a savepoint factory over the rolled-back test connection, which gives
the same semantics (an aborted inner transaction does not poison the
outer) while still discarding everything at the end of the test.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from uuid import UUID

import pandas as pd
import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.candidate_detection.config import publish_detection_configuration
from core.feature_engine.spec import publish_feature_schema_version
from core.live_scanner.scanner import ConnectionFactory
from core.live_scanner.schedule import as_of_for
from core.market_state.thresholds import publish_target_model_version
from core.model_validation_evaluation.validation.replay import ModuleConfigs
from core.outcome_tracking.config import OutcomeConfig, publish_outcome_snapshot
from core.scoring.config import publish_scoring_configuration
from core.scoring.engine import Lineage
from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from infra.db.schema.identity import universe_membership, universe_version
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)
from tests.integration.feature_engine.conftest import insert_bars

#: A plain Tuesday well clear of any holiday, and the Monday before it —
#: the two dates the catch-up tests walk.
SCAN_DATE = date(2021, 3, 2)
PREVIOUS_DATE = date(2021, 3, 1)
#: Far enough back to fill Module 08's 252-bar maximum lookback.
HISTORY_START = datetime(2019, 1, 2, tzinfo=UTC)
#: A wall-clock instant comfortably after both dates' scan times.
NOW = datetime(2021, 3, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than DELETE: nearly every table a scan touches is
    append-only or delete-guarded, so a delete-based teardown would be
    refused by Module 03's triggers.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def connect(connection: Connection) -> ConnectionFactory:
    """A savepoint per call, standing in for `engine.begin`.

    Each `with connect() as conn` gets its own nested transaction that
    commits or rolls back independently — which is what lets a test prove
    that a failed attempt's writes are discarded while the run row written
    before it survives.
    """

    @contextmanager
    def _factory() -> Iterator[Connection]:
        savepoint = connection.begin_nested()
        try:
            yield connection
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise

    return _factory


@pytest.fixture
def register(connection: Connection) -> Callable[[str], UUID]:
    resolver = SecurityIdentityResolver(connection)

    def _register(ticker: str) -> UUID:
        return resolver.register(
            ticker,
            exchange=CanonicalExchange.NASDAQ,
            valid_from=datetime(2000, 1, 1, tzinfo=UTC),
            name=f"{ticker} Test Corp.",
        )

    return _register


@pytest.fixture
def modules() -> ModuleConfigs:
    return ModuleConfigs()


@pytest.fixture
def universe_version_id(connection: Connection) -> UUID:
    return connection.execute(
        universe_version.insert()
        .values(
            version_label=f"module18-universe-{datetime.now(UTC).timestamp()}",
            as_of_date=datetime.combine(SCAN_DATE, datetime.min.time(), tzinfo=UTC),
            definition={"source": "module18 integration test"},
        )
        .returning(universe_version.c.id)
    ).scalar_one()


@pytest.fixture
def lineage(connection: Connection, modules: ModuleConfigs, universe_version_id: UUID) -> Lineage:
    """A lineage whose four code-backed halves are genuinely published.

    `publish_*` is idempotent by checksum, so this is exactly what the
    current code produces — which is what makes the default scan
    version-consistent and every deliberate mismatch in the tests a real
    one rather than a fixture artefact.
    """
    return Lineage(
        target_model_version_id=publish_target_model_version(
            connection, modules.market_state, description="module18 test"
        ),
        feature_schema_version_id=publish_feature_schema_version(
            connection, modules.features, description="module18 test"
        ),
        scoring_configuration_id=publish_scoring_configuration(
            connection, modules.scoring, description="module18 test"
        ),
        detection_configuration_id=publish_detection_configuration(
            connection, modules.detection, description="module18 test"
        ),
        universe_version_id=universe_version_id,
        data_snapshot_id=publish_outcome_snapshot(
            connection,
            OutcomeConfig(),
            as_of=as_of_for(SCAN_DATE),
            description="module18 test",
        ),
    )


def _closes(index: int, length: int) -> list[float]:
    """A long decline into a base. Deterministic, so features do not move."""
    closes: list[float] = []
    for n in range(length):
        phase = n / length
        level = 100.0 - 60.0 * (phase / 0.55) if phase < 0.55 else 40.0 + 1.5 * ((n % 9) - 4) / 4.0
        closes.append(level + index * 0.5)
    return closes


@pytest.fixture
def populated_universe(
    connection: Connection, register, lineage: Lineage
) -> Callable[..., list[UUID]]:
    """Register securities with price history and universe membership.

    Returns a callable so a test can choose how many names it wants and
    whether their bars run all the way to the scan date — the readiness
    tests need a universe whose data is deliberately incomplete.
    """

    def _populate(
        count: int = 5,
        *,
        prefix: str = "SEC",
        through: date = SCAN_DATE,
        deliver: int | None = None,
    ) -> list[UUID]:
        full_length = len(pd.bdate_range(HISTORY_START.date(), through))
        short_length = len(pd.bdate_range(HISTORY_START.date(), PREVIOUS_DATE))
        delivered = count if deliver is None else deliver

        ids: list[UUID] = []
        for index in range(count):
            security_id = register(f"{prefix}{index}")
            length = full_length if index < delivered else short_length
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

    return _populate


@pytest.fixture
def no_sleep() -> Callable[[float], None]:
    """A sleeper that records its waits instead of taking them."""

    waits: list[float] = []

    def _sleep(seconds: float) -> None:
        waits.append(seconds)

    _sleep.waits = waits  # type: ignore[attr-defined]
    return _sleep
