"""Fixtures for Module 20.

Everything worth testing here is about what the database will and will not
hand back: the approval gate is a window function over a status log, the
withdrawal guard is a set comparison against stored rows, and the whole
module's guarantee is that a filter is present. A mocked connection would
test my belief about those.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from core.candidate_detection.config import DetectionConfig, publish_detection_configuration
from core.feature_engine.spec import FeatureSpec, publish_feature_schema_version
from core.market_state.thresholds import MarketStateConfig, publish_target_model_version
from core.model_validation_evaluation.validation.review import approve, open_for_review, reject
from core.model_validation_evaluation.validation.runs import finish_run, start_run
from core.outcome_tracking.config import OutcomeConfig, publish_outcome_snapshot
from core.scoring.config import ScoringConfig, publish_scoring_configuration
from core.scoring.engine import Lineage
from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from infra.db.enums import MarketState, OutcomeStatus, ValidationRunStatus
from infra.db.schema.identity import universe_version
from infra.db.schema.setups import setup_outcomes, setups
from services.public_stats.app import create_app
from services.public_stats.config import PublicStatsConfig
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)

#: The period every fixture run covers, and the cutoff every read uses.
PERIOD_START = datetime(2025, 1, 6, tzinfo=UTC)
PERIOD_END = datetime(2025, 6, 30, tzinfo=UTC)
LIVE_START = date(2025, 7, 1)
LIVE_END = date(2025, 9, 30)
AS_OF = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def client(connection: Connection) -> Iterator[TestClient]:
    """A client whose requests run inside this test's transaction."""
    from services.public_stats.app import get_connection

    app = create_app(engine=None, config=PublicStatsConfig())  # type: ignore[arg-type]

    def _connection() -> Iterator[Connection]:
        savepoint = connection.begin_nested()
        try:
            yield connection
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise

    app.dependency_overrides[get_connection] = _connection
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


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
def reviewer(connection: Connection) -> UUID:
    """A real `users` row — the gate's reviewer column is a foreign key."""
    role_id = connection.execute(
        text("INSERT INTO roles (name) VALUES (:name) RETURNING id"),
        {"name": f"module20-{uuid4()}"},
    ).scalar_one()
    return connection.execute(
        text(
            "INSERT INTO users (email, display_name, role_id) "
            "VALUES (:email, 'Module 20 Reviewer', :role) RETURNING id"
        ),
        {"email": f"reviewer-{uuid4()}@example.test", "role": role_id},
    ).scalar_one()


@pytest.fixture
def lineage(connection: Connection) -> Lineage:
    """A published six-part lineage, so a validation run can be recorded."""
    universe_id = connection.execute(
        universe_version.insert()
        .values(
            version_label=f"module20-{uuid4()}",
            as_of_date=PERIOD_START,
            definition={"source": "module20 test"},
        )
        .returning(universe_version.c.id)
    ).scalar_one()

    return Lineage(
        target_model_version_id=publish_target_model_version(connection, MarketStateConfig()),
        feature_schema_version_id=publish_feature_schema_version(connection, FeatureSpec()),
        scoring_configuration_id=publish_scoring_configuration(connection, ScoringConfig()),
        detection_configuration_id=publish_detection_configuration(connection, DetectionConfig()),
        universe_version_id=universe_id,
        data_snapshot_id=publish_outcome_snapshot(connection, OutcomeConfig(), as_of=PERIOD_START),
    )


@pytest.fixture
def make_run(connection: Connection, lineage: Lineage) -> Callable[..., UUID]:
    """A COMPLETED validation run, optionally moved through the review gate."""

    def _make(
        *,
        status: str | None = None,
        period_start: datetime = PERIOD_START,
        period_end: datetime = PERIOD_END,
        reviewer_id: UUID | None = None,
    ) -> UUID:
        run = start_run(
            connection,
            lineage=lineage,
            period_start=period_start,
            period_end=period_end,
        )
        finish_run(connection, run.id, status=ValidationRunStatus.COMPLETED)
        if status is None:
            return run.id
        open_for_review(connection, run.id)
        if status == "APPROVED":
            approve(connection, run.id, user_id=reviewer_id, note="module20 fixture")
        elif status == "REJECTED":
            reject(connection, run.id, user_id=reviewer_id, note="module20 fixture")
        return run.id

    return _make


@pytest.fixture
def seed_outcomes(
    connection: Connection, lineage: Lineage, register: Callable[[str], UUID]
) -> Callable[..., list[UUID]]:
    """Concluded setups with outcomes, inside a named period.

    Written directly rather than driven through the pipeline: this module
    is a consumer of stored outcomes, and building them through a full
    replay would make a public-stats test fail whenever detection changed.
    """

    def _seed(
        count: int,
        *,
        prefix: str,
        detected_from: datetime = PERIOD_START,
        outcome_status: OutcomeStatus = OutcomeStatus.SUCCESS,
        relative_return: float = 0.20,
        mfe: float = 0.35,
        mae: float = -0.08,
        regime: MarketState = MarketState.UPTREND,
        qualified: bool = True,
        data_snapshot_id: UUID | None = None,
    ) -> list[UUID]:
        from core.scoring.engine import score_candidate
        from core.scoring.persistence import write_signal
        from tests.unit.scoring.factories import adequate, scoring_inputs

        created: list[UUID] = []
        for index in range(count):
            security_id = register(f"{prefix}{index:03d}")
            detected = detected_from + timedelta(days=index)
            signal_id = None
            if qualified:
                signal = score_candidate(
                    scoring_inputs(security_id, cross=adequate()),
                    as_of=detected,
                    lineage=lineage,
                )
                signal_id = write_signal(connection, signal)

            setup_id = connection.execute(
                setups.insert()
                .values(
                    security_id=security_id,
                    detected_at=detected,
                    target_model_version_id=lineage.target_model_version_id,
                    detection_configuration_id=lineage.detection_configuration_id,
                    universe_version_id=lineage.universe_version_id,
                    feature_schema_version_id=lineage.feature_schema_version_id,
                    qualifying_signal_id=signal_id,
                    concluded_at=detected + timedelta(days=60),
                )
                .returning(setups.c.id)
            ).scalar_one()

            connection.execute(
                setup_outcomes.insert().values(
                    setup_id=setup_id,
                    outcome_status=outcome_status.value,
                    mfe=mfe,
                    mae=mae,
                    realized_return=relative_return + 0.05,
                    benchmark_relative_return=relative_return,
                    time_to_mfe=timedelta(days=30),
                    outcome_window=timedelta(days=60),
                    market_regime_at_outcome=regime.value,
                    data_snapshot_id=data_snapshot_id or lineage.data_snapshot_id,
                    recorded_at=detected + timedelta(days=61),
                )
            )
            created.append(setup_id)
        return created

    return _seed
