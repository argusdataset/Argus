"""Database fixtures for Module 12.

Everything this module claims is a claim about what the database holds and
what a query at a past date can see, so these run against a real
PostgreSQL — the same reasoning Modules 03, 07, 09 and 10 established. A
mock would happily return whatever the leakage test expected it to.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.candidate_detection.config import DetectionConfig, publish_detection_configuration
from core.risk_context.events import EARNINGS
from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from infra.db.enums import EligibilityGate
from infra.db.schema.intelligence import eligibility_check_results, pending_material_events
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than DELETE: `eligibility_check_results` is
    append-only, so a delete-based teardown would be refused by Module
    03's guard.
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
def add_event(connection: Connection) -> Callable[..., None]:
    """Write one material event with explicit PIT timestamps.

    Deliberately raw rather than going through `translate_earnings_event`:
    the leakage tests need to set `availability_time` to a specific past
    instant, and a helper that derived it would be testing the deriver
    instead of the query.
    """

    def _add(
        security_id: UUID,
        *,
        scheduled_for: datetime,
        available: datetime,
        observed: datetime | None = None,
        event_type: str = EARNINGS,
        is_binary: bool = True,
        details: dict[str, Any] | None = None,
        source: str = "test",
    ) -> None:
        observed = observed or available
        connection.execute(
            pending_material_events.insert().values(
                security_id=security_id,
                event_type=event_type,
                scheduled_for=scheduled_for,
                is_binary=is_binary,
                event_time=observed,
                observation_time=observed,
                availability_time=available,
                ingestion_time=observed,
                details=details or {},
                source=source,
            )
        )

    return _add


@pytest.fixture
def detection_configuration_id(connection: Connection) -> UUID:
    return publish_detection_configuration(
        connection, DetectionConfig(), description="module12 integration test"
    )


@pytest.fixture
def record_eligibility(
    connection: Connection, detection_configuration_id: UUID
) -> Callable[..., UUID]:
    """Write one full eligibility run for a security. Returns its run_id.

    Writes every gate, passes included — which is exactly what makes the
    re-evaluation signal computable, and is Module 09's own decision (see
    `core/candidate_detection/persistence.py`).
    """

    def _record(
        security_id: UUID,
        *,
        evaluated_at: datetime,
        failed: tuple[EligibilityGate, ...] = (),
        run_id: UUID | None = None,
        gates: tuple[EligibilityGate, ...] | None = None,
    ) -> UUID:
        run_id = run_id or uuid4()
        for gate in gates or tuple(EligibilityGate):
            connection.execute(
                eligibility_check_results.insert().values(
                    run_id=run_id,
                    security_id=security_id,
                    gate=gate.value,
                    passed=gate not in failed,
                    detail={"source": "module12 integration test"},
                    detection_configuration_id=detection_configuration_id,
                    evaluated_at=evaluated_at,
                )
            )
        return run_id

    return _record
