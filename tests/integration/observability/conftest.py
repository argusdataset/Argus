"""Fixtures for Module 23.

Real PostgreSQL throughout. The four-state freshness model is a set of
comparisons against columns, the ordering probe is a GROUP BY HAVING, and
the health check reads `alembic_version` — all three would pass against a
mock that returned whatever the test expected.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.candidate_detection.config import DetectionConfig, publish_detection_configuration
from core.feature_engine.spec import FeatureSpec, publish_feature_schema_version
from core.market_state.thresholds import MarketStateConfig, publish_target_model_version
from core.outcome_tracking.config import OutcomeConfig, publish_outcome_snapshot
from core.scoring.config import ScoringConfig, publish_scoring_configuration
from core.scoring.engine import Lineage
from data.canonical_model.exchanges import CanonicalExchange
from data.canonical_model.records import CanonicalTimeframe
from data.normalization.identity import SecurityIdentityResolver
from infra.db.enums import LiveScanStatus, MarketState
from infra.db.schema.canonical import canonical_ohlcv
from infra.db.schema.identity import universe_version
from infra.db.schema.intelligence import market_state, market_state_transitions
from infra.db.schema.live_scanner import live_scan_runs
from infra.observability.config import ObservabilityConfig
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def config() -> ObservabilityConfig:
    return ObservabilityConfig()


@pytest.fixture
def register(connection: Connection) -> Callable[..., UUID]:
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
def model_version(connection: Connection) -> UUID:
    return publish_target_model_version(connection, MarketStateConfig())


@pytest.fixture
def add_bar(connection: Connection) -> Callable[..., None]:
    """One canonical bar with explicit PIT timestamps.

    The timestamps are the whole point: freshness is measured on
    `availability_time`, and the ingestion lag on the gap between that and
    `event_time`, so both are set per call rather than defaulted.
    """

    def _add(
        security_id: UUID,
        *,
        event_time: datetime,
        available_at: datetime | None = None,
        close: float = 100.0,
    ) -> None:
        stamp = available_at or event_time + timedelta(hours=1)
        price = Decimal(str(close))
        connection.execute(
            canonical_ohlcv.insert().values(
                security_id=security_id,
                timeframe=CanonicalTimeframe.DAILY.value,
                event_time=event_time,
                observation_time=stamp,
                availability_time=stamp,
                ingestion_time=stamp,
                open_raw=price,
                high_raw=price,
                low_raw=price,
                close_raw=price,
                volume_raw=1_000_000,
            )
        )

    return _add


@pytest.fixture
def add_transition(connection: Connection, model_version: UUID) -> Callable[..., None]:
    """A recorded state change, with Module 10's `unclassified_reason` intact."""

    def _add(
        security_id: UUID,
        *,
        to_state: MarketState = MarketState.UNCLASSIFIED,
        from_state: MarketState | None = MarketState.CONSOLIDATION,
        at: datetime,
        reason: str | None = None,
    ) -> None:
        evidence: dict[str, object] = {"backward_transition": False}
        if reason is not None:
            evidence["unclassified_reason"] = reason
        connection.execute(
            market_state_transitions.insert().values(
                security_id=security_id,
                from_state=from_state.value if from_state else None,
                to_state=to_state.value,
                transition_time=at,
                duration_in_prior_state=timedelta(days=10) if from_state else None,
                confidence=0.6,
                evidence=evidence,
                target_model_version_id=model_version,
            )
        )

    return _add


@pytest.fixture
def set_state(connection: Connection, model_version: UUID) -> Callable[..., None]:
    def _set(security_id: UUID, state: MarketState) -> None:
        connection.execute(
            market_state.insert().values(
                security_id=security_id,
                state=state.value,
                entered_at=NOW - timedelta(days=3),
                confidence=0.5,
                target_model_version_id=model_version,
            )
        )

    return _set


@pytest.fixture
def lineage(connection: Connection) -> Lineage:
    """The six configuration versions `live_scan_runs` makes mandatory.

    Mandatory because Module 03 decided a run whose configuration cannot
    be recovered is a run nobody can check — so even an observability
    fixture has to supply them.
    """
    universe_id = connection.execute(
        universe_version.insert()
        .values(
            version_label=f"module23-{uuid4()}",
            as_of_date=NOW,
            definition={"source": "module23 test"},
        )
        .returning(universe_version.c.id)
    ).scalar_one()

    return Lineage(
        target_model_version_id=publish_target_model_version(connection, MarketStateConfig()),
        feature_schema_version_id=publish_feature_schema_version(connection, FeatureSpec()),
        scoring_configuration_id=publish_scoring_configuration(connection, ScoringConfig()),
        detection_configuration_id=publish_detection_configuration(connection, DetectionConfig()),
        universe_version_id=universe_id,
        data_snapshot_id=publish_outcome_snapshot(connection, OutcomeConfig(), as_of=NOW),
    )


@pytest.fixture
def add_scan_run(connection: Connection, lineage: Lineage) -> Callable[..., UUID]:
    """A `live_scan_runs` row, so Module 18's feed has something to hand back."""

    def _add(
        *,
        scan_date: date,
        status: LiveScanStatus,
        as_of: datetime | None = None,
        attempt: int = 1,
        finished_at: datetime | None = None,
        excluded: list[str] | None = None,
        note: str | None = None,
    ) -> UUID:
        return connection.execute(
            live_scan_runs.insert()
            .values(
                scan_date=scan_date,
                as_of=as_of
                or datetime(scan_date.year, scan_date.month, scan_date.day, 21, tzinfo=UTC),
                attempt=attempt,
                status=status.value,
                excluded_securities=excluded or [],
                detail={},
                note=note,
                finished_at=finished_at,
                target_model_version_id=lineage.target_model_version_id,
                feature_schema_version_id=lineage.feature_schema_version_id,
                scoring_configuration_id=lineage.scoring_configuration_id,
                detection_configuration_id=lineage.detection_configuration_id,
                universe_version_id=lineage.universe_version_id,
                data_snapshot_id=lineage.data_snapshot_id,
            )
            .returning(live_scan_runs.c.id)
        ).scalar_one()

    return _add


@pytest.fixture
def uid() -> Callable[[], str]:
    def _uid() -> str:
        return str(uuid4())

    return _uid
