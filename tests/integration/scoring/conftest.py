"""Database fixtures for Module 13.

The scoring arithmetic is pure and is tested in `tests/unit/scoring/`.
What needs a real PostgreSQL is everything around it: the append-only
`scoring_configuration` row, the six-way foreign-keyed lineage, and Module
03's CHECK constraint that a SCORED row carries all four numbers and an
INSUFFICIENT_EVIDENCE row carries none. That constraint is the structural
guarantee this whole module is built around, and only the database
enforces it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.candidate_detection.config import DetectionConfig, publish_detection_configuration
from core.scoring.config import ScoringConfig, publish_scoring_configuration
from core.scoring.engine import Lineage
from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from infra.db.schema.identity import universe_version
from infra.db.schema.versioning import (
    data_snapshot,
    feature_schema_version,
    target_model_version,
)
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)

AS_OF = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than DELETE: `signals` and `scoring_configuration` are
    both append-only, so a delete-based teardown would be refused by
    Module 03's guard.
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
def scoring_configuration_id(connection: Connection) -> UUID:
    return publish_scoring_configuration(
        connection, ScoringConfig(), description="module13 integration test"
    )


@pytest.fixture
def lineage(connection: Connection, scoring_configuration_id: UUID) -> Lineage:
    """A full six-part lineage backed by real rows.

    Every one of the six is a NOT NULL foreign key on `signals`, so a
    scoring result cannot be persisted without all six existing — which is
    Module 03 making reproducibility structural rather than a convention.
    """
    label = uuid4()

    def _version(table, **extra) -> UUID:
        return connection.execute(
            table.insert()
            .values(
                version_label=f"module13-test-{table.name}-{label}",
                definition={"source": "module13 integration test"},
                content_checksum=f"{table.name}-{label}",
                **extra,
            )
            .returning(table.c.id)
        ).scalar_one()

    return Lineage(
        target_model_version_id=_version(target_model_version),
        feature_schema_version_id=_version(feature_schema_version),
        data_snapshot_id=_version(data_snapshot, as_of_time=AS_OF),
        scoring_configuration_id=scoring_configuration_id,
        universe_version_id=connection.execute(
            universe_version.insert()
            .values(
                version_label=f"module13-test-universe-{label}",
                as_of_date=AS_OF,
                definition={"source": "module13 integration test"},
            )
            .returning(universe_version.c.id)
        ).scalar_one(),
        detection_configuration_id=publish_detection_configuration(
            connection, DetectionConfig(), description="module13 integration test"
        ),
    )
