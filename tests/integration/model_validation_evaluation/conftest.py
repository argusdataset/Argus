"""Database fixtures for Module 17.

Almost everything here needs a real PostgreSQL, and unusually so: this
module's job is to orchestrate ten other modules against real tables, and
a stubbed connection would prove that the orchestration compiles rather
than that it works. The version-consistency check in particular is
*entirely* about what is stored — its whole mechanism is comparing a
recomputed checksum against a published row.

The lineage below is published through each module's own `publish_*`
function rather than inserted by hand. That matters: those functions are
idempotent by `content_checksum`, and the code-drift check compares
against exactly that checksum. A hand-inserted row with a made-up
checksum would make every replay in these tests refuse — correctly, which
is itself worth knowing, and is asserted in `test_version_consistency.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.engine import Connection

from core.candidate_detection.config import publish_detection_configuration
from core.feature_engine.spec import publish_feature_schema_version
from core.market_state.thresholds import MarketStateConfig, publish_target_model_version
from core.model_validation_evaluation.validation.replay import ModuleConfigs
from core.outcome_tracking.config import OutcomeConfig, publish_outcome_snapshot
from core.scoring.config import publish_scoring_configuration
from core.scoring.engine import Lineage
from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from infra.db.schema.identity import universe_version
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)

#: The replay period the fixtures are built around. Deliberately historical
#: — nothing in Module 17 reads a wall clock, and a test period anchored
#: to "now" would drift into a different calendar every year.
PERIOD_START = datetime(2021, 1, 4, 21, 0, tzinfo=UTC)
PERIOD_END = datetime(2021, 3, 31, 21, 0, tzinfo=UTC)
AS_OF = datetime(2022, 6, 1, 21, 0, tzinfo=UTC)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than DELETE: nearly every table Module 17 touches is
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
    """Default configurations for every module the replay drives."""
    return ModuleConfigs()


@pytest.fixture
def universe_version_id(connection: Connection) -> UUID:
    return connection.execute(
        universe_version.insert()
        .values(
            version_label=f"module17-test-universe-{uuid4()}",
            as_of_date=PERIOD_START,
            definition={"source": "module17 integration test"},
        )
        .returning(universe_version.c.id)
    ).scalar_one()


@pytest.fixture
def lineage(connection: Connection, modules: ModuleConfigs, universe_version_id: UUID) -> Lineage:
    """A six-part lineage whose four code-backed halves are really published.

    `publish_*` is idempotent by checksum, so this lineage is exactly what
    the current code produces — which is what makes the default replay
    version-consistent and every deliberate mismatch in the tests a real
    mismatch rather than an artefact of the fixture.
    """
    return Lineage(
        target_model_version_id=publish_target_model_version(
            connection, modules.market_state, description="module17 integration test"
        ),
        feature_schema_version_id=publish_feature_schema_version(
            connection, modules.features, description="module17 integration test"
        ),
        scoring_configuration_id=publish_scoring_configuration(
            connection, modules.scoring, description="module17 integration test"
        ),
        detection_configuration_id=publish_detection_configuration(
            connection, modules.detection, description="module17 integration test"
        ),
        universe_version_id=universe_version_id,
        data_snapshot_id=publish_outcome_snapshot(
            connection,
            modules.outcome,
            as_of=AS_OF,
            description="module17 integration test",
        ),
    )


@pytest.fixture
def second_snapshot(connection: Connection) -> UUID:
    """A second `data_snapshot`, standing for a revised success criterion.

    Correction 1's whole point: a recomputation travels with a new
    snapshot. The criterion itself is not changed here — that would mean
    editing Module 15's config — but the snapshot's cutoff is, which is
    the other half of what a snapshot pins and is enough to exercise the
    uniqueness key.
    """
    return publish_outcome_snapshot(
        connection,
        OutcomeConfig(),
        as_of=AS_OF + timedelta(days=180),
        description="module17 revised-criterion stand-in",
    )


@pytest.fixture
def other_target_model(connection: Connection) -> UUID:
    """A second published `target_model_version`, for re-score tests.

    Published through the real function with a genuinely different
    configuration, so its checksum differs from the current code's —
    which is exactly the situation the code-drift check must catch.
    """
    from core.market_state.thresholds import StateThresholds

    return publish_target_model_version(
        connection,
        MarketStateConfig(target_model_name="target-model-v1-alternate", states=StateThresholds()),
        description="module17 alternate model",
    )


@pytest.fixture
def seed_setup(connection: Connection, lineage: Lineage) -> Callable[..., UUID]:
    """Insert a concluded setup with an outcome, the way Modules 14/15 would.

    Direct SQL rather than driving the whole pipeline: these fixtures
    exist to give the *evaluation* half a population, and building one
    through a full replay would make an evaluation test fail whenever
    detection changed. The replay path has its own end-to-end test.
    """
    from infra.db.enums import MarketState, OutcomeStatus
    from infra.db.schema.setups import setup_outcomes, setups

    def _seed(
        security_id: UUID,
        *,
        detected_at: datetime = PERIOD_START,
        concluded_at: datetime | None = None,
        outcome_status: OutcomeStatus = OutcomeStatus.SUCCESS,
        relative_return: float = 0.20,
        mfe: float = 0.30,
        mae: float = -0.06,
        regime: MarketState = MarketState.UPTREND,
        data_snapshot_id: UUID | None = None,
        qualifying_signal_id: UUID | None = None,
    ) -> UUID:
        concluded = concluded_at or detected_at + timedelta(days=60)
        setup_id = connection.execute(
            setups.insert()
            .values(
                security_id=security_id,
                detected_at=detected_at,
                target_model_version_id=lineage.target_model_version_id,
                detection_configuration_id=lineage.detection_configuration_id,
                universe_version_id=lineage.universe_version_id,
                feature_schema_version_id=lineage.feature_schema_version_id,
                qualifying_signal_id=qualifying_signal_id,
                concluded_at=concluded,
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
                recorded_at=concluded + timedelta(days=1),
            )
        )
        return setup_id

    return _seed


@pytest.fixture
def count_queries() -> Callable[[Connection], AbstractContextManager[list[str]]]:
    """A context manager recording every statement a connection executes.

    Used by the batch-scale test, which proves the engine's cost per scan
    date does not grow per security. A SQLAlchemy `before_cursor_execute`
    listener rather than `pg_stat_statements`, which is an extension that
    may not be installed — and a scale test that silently skipped would be
    worse than none.
    """

    @contextmanager
    def _counter(conn: Connection) -> Iterator[list[str]]:
        statements: list[str] = []

        def record(_conn, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(conn.engine, "before_cursor_execute", record)
        try:
            yield statements
        finally:
            event.remove(conn.engine, "before_cursor_execute", record)

    return _counter
