"""Fixtures for Module 21.

The assertions worth making here are all about what the database hands
back: whether a watchlist reflects a state change with no refresh step,
whether an unscored candidate produces a complete response, whether
cross-asset and same-asset stay apart. A mocked connection would test my
beliefs about those.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.candidate_detection.config import DetectionConfig, publish_detection_configuration
from core.feature_engine.spec import FeatureSpec, publish_feature_schema_version
from core.lifecycle.derivation import advance
from core.lifecycle.engine import open_setup
from core.lifecycle.events import ACTIVATED as ACTIVATED_EVENT
from core.lifecycle.events import ENDPOINT_REACHED, QUALIFIED
from core.market_state.thresholds import MarketStateConfig, publish_target_model_version
from core.outcome_tracking.config import OutcomeConfig, publish_outcome_snapshot
from core.outcome_tracking.engine import compute_case, record_outcome
from core.scoring.config import ScoringConfig, publish_scoring_configuration
from core.scoring.engine import Lineage
from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from infra.db.enums import AnalogueScope, MarketState, SetupLifecycleStatus
from infra.db.schema.identity import universe_version
from infra.db.schema.intelligence import (
    historical_similarity_results,
    market_state,
    market_state_transitions,
    pending_material_events,
)
from services.intelligence.app import create_app
from services.intelligence.config import IntelligenceConfig
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)
from tests.integration.outcome_tracking.conftest import write_bars

#: Anchored to the wall clock rather than a fixed date, because the
#: endpoints under test call `datetime.now(UTC)` themselves. A frozen
#: fixture date would make every response permanently stale and the
#: freshness assertions meaningless.
NOW = datetime.now(UTC).replace(microsecond=0)

#: Bars for the concluded-setup fixture. Far enough back that the whole
#: outcome window has closed by `NOW`.
FIRST_BAR = datetime(2024, 1, 2, tzinfo=UTC)
DETECTED = datetime(2024, 1, 16, 21, 0, tzinfo=UTC)
ACTIVATED = datetime(2024, 1, 30, 21, 0, tzinfo=UTC)
TERMINAL = datetime(2024, 3, 26, 21, 0, tzinfo=UTC)

#: Rises past the success threshold well inside the horizon.
WINNER = [100.0 * (1.004**step) for step in range(120)]
#: Falls past the failure threshold shortly after activation.
LOSER = [100.0 * (0.997**step) for step in range(120)]


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
    from services.intelligence.app import get_connection

    app = create_app(engine=None, config=IntelligenceConfig())  # type: ignore[arg-type]

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
def register(connection: Connection) -> Callable[..., UUID]:
    resolver = SecurityIdentityResolver(connection)

    def _register(ticker: str, *, name: str | None = None) -> UUID:
        return resolver.register(
            ticker,
            exchange=CanonicalExchange.NASDAQ,
            valid_from=datetime(2000, 1, 1, tzinfo=UTC),
            name=name or f"{ticker} Test Corp.",
        )

    return _register


@pytest.fixture
def lineage(connection: Connection) -> Lineage:
    universe_id = connection.execute(
        universe_version.insert()
        .values(
            version_label=f"module21-{uuid4()}",
            as_of_date=NOW,
            definition={"source": "module21 test"},
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
def set_state(connection: Connection, lineage: Lineage) -> Callable[..., None]:
    """Put a security into a market state, in the projection.

    Writes the projection directly rather than driving Module 10's
    classifier: this module reads the projection, and building states
    through a full classification would make an Intelligence test fail
    whenever a threshold moved.
    """

    def _set(
        security_id: UUID,
        state: MarketState,
        *,
        entered_at: datetime = NOW - timedelta(days=10),
        confidence: float | None = 0.6,
    ) -> None:
        existing = connection.execute(
            market_state.select().where(market_state.c.security_id == security_id)
        ).one_or_none()
        values = {
            "state": state.value,
            "entered_at": entered_at,
            "confidence": confidence,
            "target_model_version_id": lineage.target_model_version_id,
        }
        if existing is None:
            connection.execute(market_state.insert().values(security_id=security_id, **values))
        else:
            connection.execute(
                market_state.update()
                .where(market_state.c.security_id == security_id)
                .values(**values)
            )

    return _set


@pytest.fixture
def add_transition(connection: Connection, lineage: Lineage) -> Callable[..., None]:
    """One recorded state change — the chart's marks come from these."""

    def _add(
        security_id: UUID,
        *,
        to_state: MarketState,
        from_state: MarketState | None = None,
        at: datetime,
        backward: bool = False,
        prior_duration: timedelta = timedelta(days=30),
    ) -> None:
        connection.execute(
            market_state_transitions.insert().values(
                security_id=security_id,
                from_state=from_state.value if from_state else None,
                to_state=to_state.value,
                transition_time=at,
                # Module 03 requires this whenever a prior state exists:
                # how long the security sat in it is a feature Module 08
                # reads, so the transition row carries it rather than
                # leaving it to be recomputed.
                duration_in_prior_state=prior_duration if from_state else None,
                confidence=0.6,
                evidence={"backward_transition": backward},
                target_model_version_id=lineage.target_model_version_id,
            )
        )

    return _add


@pytest.fixture
def score(connection: Connection, lineage: Lineage) -> Callable[..., UUID | None]:
    """Write a real signal through Module 13's own engine and writer.

    Real rather than a hand-built row: the response this module serves is
    a reshaping of what Module 13 stored, and a fabricated row would let a
    reshaping bug pass.
    """

    def _score(
        security_id: UUID,
        *,
        strong: bool = True,
        at: datetime = NOW - timedelta(days=1),
    ) -> UUID | None:
        from core.scoring.engine import score_candidate
        from core.scoring.persistence import write_signal
        from tests.unit.scoring.factories import adequate, insufficient, scoring_inputs

        signal = score_candidate(
            scoring_inputs(security_id, cross=adequate() if strong else insufficient()),
            as_of=at,
            lineage=lineage,
        )
        if not signal.writes_signal:
            return None
        return write_signal(connection, signal)

    return _score


@pytest.fixture
def add_similarity(connection: Connection, lineage: Lineage) -> Callable[..., None]:
    """A stored Module 11 result for one scope."""

    def _add(
        security_id: UUID,
        *,
        scope: AnalogueScope,
        match_count: int,
        sufficiency: str,
        median_outcome: float | None = 0.18,
        failure_rate: float | None = 0.35,
        at: datetime = NOW - timedelta(days=1),
    ) -> None:
        connection.execute(
            historical_similarity_results.insert().values(
                security_id=security_id,
                event_time=at,
                scope=scope.value,
                similar_setup_count=match_count,
                similarity_distribution={
                    "sufficiency": sufficiency,
                    "failure_rate_interval": {"low": 0.2, "high": 0.5},
                },
                median_outcome=median_outcome,
                average_outcome=median_outcome,
                failure_rate=failure_rate,
                outcome_by_regime={"UPTREND": {"count": match_count}},
                feature_schema_version_id=lineage.feature_schema_version_id,
                data_snapshot_id=lineage.data_snapshot_id,
                computed_at=at,
            )
        )

    return _add


@pytest.fixture
def concluded_setup(connection: Connection, lineage: Lineage) -> Callable[..., UUID]:
    """A setup driven to a terminal event and given an outcome row.

    Built through Modules 14 and 15's own functions rather than by
    inserting rows: the "why did this fail" endpoint hands the setup back
    to `compute_case`, and a hand-written outcome row would let the
    endpoint pass while the real assembly path was broken.
    """

    def _build(
        security_id: UUID,
        closes: list[float],
        *,
        terminal_event: str = ENDPOINT_REACHED,
    ) -> UUID:
        write_bars(connection, security_id, closes, start=FIRST_BAR)
        setup_id, _ = open_setup(connection, security_id, as_of=DETECTED, lineage=lineage)
        advance(
            connection,
            setup_id,
            to=SetupLifecycleStatus.QUALIFICATION,
            event_type=QUALIFIED,
            occurred_at=DETECTED + timedelta(days=1),
            payload={"market_state": "CONSOLIDATION"},
        )
        advance(
            connection,
            setup_id,
            to=SetupLifecycleStatus.ACTIVE,
            event_type=ACTIVATED_EVENT,
            occurred_at=ACTIVATED,
            payload={"market_state": "BREAKOUT_READY"},
        )
        advance(
            connection,
            setup_id,
            to=SetupLifecycleStatus.OUTCOME,
            event_type=terminal_event,
            occurred_at=TERMINAL,
            payload={
                "market_state": "UPTREND",
                "status_before": SetupLifecycleStatus.ACTIVE.value,
            },
        )
        return setup_id

    return _build


@pytest.fixture
def conclude(connection: Connection, lineage: Lineage) -> Callable[..., None]:
    """Run Module 15 over a setup and store the outcome it produced."""

    def _conclude(setup_id: UUID) -> None:
        case = compute_case(
            connection,
            setup_id,
            as_of=NOW,
            data_snapshot_id=lineage.data_snapshot_id,
        )
        record_outcome(connection, case, data_snapshot_id=lineage.data_snapshot_id)

    return _conclude


@pytest.fixture
def supersede(connection: Connection, lineage: Lineage) -> Callable[..., UUID]:
    """File a correction over an existing signal, the way Module 13 does.

    A new row pointing at the one it replaces — never an edit, because
    `signals` is append-only. What makes it worth a fixture is that the
    withdrawn row stays in the table and stays readable, so a reader that
    forgot the filter would find it and serve it.
    """

    def _supersede(
        security_id: UUID,
        original_id: UUID,
        *,
        at: datetime = NOW,
    ) -> UUID:
        from core.scoring.engine import score_candidate
        from core.scoring.persistence import write_signal
        from tests.unit.scoring.factories import insufficient, scoring_inputs

        correction = score_candidate(
            scoring_inputs(security_id, cross=insufficient()),
            as_of=at,
            lineage=lineage,
        )
        return write_signal(connection, correction, supersedes=original_id)

    return _supersede


@pytest.fixture
def schedule_event(connection: Connection) -> Callable[..., None]:
    """A material event with its own announcement time.

    `announced_at` is separate from `scheduled_for` on purpose: a date
    ARGUS learns next week must not appear in today's answer, and the two
    timestamps are what makes that testable rather than assumed.
    """

    def _schedule(
        security_id: UUID,
        *,
        scheduled_for: datetime,
        announced_at: datetime,
        event_type: str = "EARNINGS",
        is_binary: bool = True,
    ) -> None:
        connection.execute(
            pending_material_events.insert().values(
                security_id=security_id,
                event_type=event_type,
                scheduled_for=scheduled_for,
                is_binary=is_binary,
                event_time=announced_at,
                observation_time=announced_at,
                availability_time=announced_at,
                ingestion_time=announced_at,
                details={"source": "module21 test"},
                source="TEST",
            )
        )

    return _schedule
