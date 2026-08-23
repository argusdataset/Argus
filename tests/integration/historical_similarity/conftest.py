"""Fixtures for Module 11's database-backed tests.

The case dataset is a three-table join (`setups`, `setup_outcomes`,
`feature_vectors`) with two independent PIT cutoffs, so it runs against a
real PostgreSQL. A mock would test my belief about the join rather than
the join.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.candidate_detection.config import DetectionConfig, publish_detection_configuration
from core.feature_engine.spec import FeatureSpec, publish_feature_schema_version
from core.historical_similarity.config import (
    SimilarityConfig,
    publish_similarity_configuration,
)
from core.market_state.thresholds import MarketStateConfig, publish_target_model_version
from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from infra.db.enums import MarketState, OutcomeStatus
from infra.db.schema.identity import universe_version
from infra.db.schema.setups import setup_outcomes, setups
from infra.db.schema.versioning import feature_vectors
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


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
def schema_version_id(connection: Connection) -> UUID:
    return publish_feature_schema_version(connection, FeatureSpec())


@pytest.fixture
def version_ids(connection: Connection) -> dict[str, UUID]:
    """The three version rows a `setups` row needs as foreign keys."""
    universe_id = connection.execute(
        universe_version.insert()
        .values(
            version_label=f"m11-test-{uuid4()}",
            as_of_date=AS_OF,
            definition={"source": "module11 integration test"},
        )
        .returning(universe_version.c.id)
    ).scalar_one()
    return {
        "universe": universe_id,
        "target_model": publish_target_model_version(connection, MarketStateConfig()),
        "detection": publish_detection_configuration(connection, DetectionConfig()),
        # Migration 0006 made setup_outcomes.data_snapshot_id NOT NULL:
        # an outcome nobody can re-derive is the one result in ARGUS that
        # must not exist.
        "snapshot": publish_similarity_configuration(
            connection, SimilarityConfig(), as_of=AS_OF
        ),
    }


@pytest.fixture
def make_case(
    connection: Connection, schema_version_id: UUID, version_ids: dict[str, UUID]
) -> Callable[..., UUID]:
    """Create one complete historical case: setup + features + outcome.

    `recorded_at` defaults to well before `AS_OF` so the case counts as
    concluded; tests that exercise the PIT cutoff override it.
    """

    def _make(
        security_id: UUID,
        features: dict[str, Any],
        *,
        detected_at: datetime = datetime(2023, 1, 10, tzinfo=UTC),
        recorded_at: datetime | None = None,
        outcome_status: OutcomeStatus = OutcomeStatus.SUCCESS,
        realized_return: float = 0.25,
        mfe: float = 0.40,
        mae: float = -0.09,
        regime: MarketState = MarketState.UPTREND,
        feature_availability: datetime | None = None,
    ) -> UUID:
        concluded = recorded_at or datetime(2023, 6, 1, tzinfo=UTC)
        setup_id = connection.execute(
            setups.insert()
            .values(
                security_id=security_id,
                detected_at=detected_at,
                target_model_version_id=version_ids["target_model"],
                detection_configuration_id=version_ids["detection"],
                universe_version_id=version_ids["universe"],
                # A case *is* a concluded setup — it has an outcome. Set
                # explicitly so the fixture states that rather than
                # leaving these looking permanently open, which migration
                # 0006's one-open-setup-per-security index would refuse
                # for a security with more than one case.
                concluded_at=concluded,
            )
            .returning(setups.c.id)
        ).scalar_one()

        available = feature_availability or (detected_at - timedelta(days=1))
        connection.execute(
            feature_vectors.insert().values(
                security_id=security_id,
                feature_schema_version_id=schema_version_id,
                event_time=available,
                availability_time=available,
                features=features,
            )
        )

        connection.execute(
            setup_outcomes.insert().values(
                setup_id=setup_id,
                outcome_status=outcome_status.value,
                mfe=mfe,
                mae=mae,
                realized_return=realized_return,
                time_to_mfe=timedelta(days=45),
                outcome_window=timedelta(days=90),
                market_regime_at_outcome=regime.value,
                data_snapshot_id=version_ids["snapshot"],
                recorded_at=concluded,
            )
        )
        return setup_id

    return _make
