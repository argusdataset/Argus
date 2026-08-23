"""Database fixtures for Module 14.

The lifecycle's arithmetic is one function over a list of events and is
tested without a database in `tests/unit/lifecycle/`. What needs a real
PostgreSQL is everything that makes the event log trustworthy: the
append-only guard, the per-setup sequence constraint, and the fact that
`setups` has no status column to drift from the log.

Module 13's own factories build the signals, so the lifecycle is driven by
what the scoring engine actually produces rather than by a hand-built
imitation of it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.risk_context.invalidation import EligibilityTrend
from core.scoring.engine import Lineage, ScoredSignal, score_candidate
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)
from tests.integration.scoring.conftest import (  # noqa: F401
    lineage,
    register,
    scoring_configuration_id,
)
from tests.unit.scoring.factories import adequate, insufficient, scoring_inputs

AS_OF = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than DELETE: `setup_events` is append-only and
    `setups` refuses DELETE, so a delete-based teardown would be refused
    by Module 03's guards.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def scored(lineage: Lineage) -> Callable[..., ScoredSignal]:  # noqa: F811
    """A Module 13 result for one security, run through the real engine.

    `strong=True` gives a candidate with adequate historical evidence, so
    it actually gets scored — which today requires a fixture, because a
    real scan produces nothing but refusals.
    """

    def _scored(
        security_id: UUID,
        *,
        strong: bool = True,
        gated: bool = False,
        as_of: datetime = AS_OF,
        **kwargs,
    ) -> ScoredSignal:
        if gated:
            kwargs["eligibility_trend"] = EligibilityTrend.LOST_ELIGIBILITY
        return score_candidate(
            scoring_inputs(
                security_id,
                cross=adequate() if strong else insufficient(),
                **kwargs,
            ),
            as_of=as_of,
            lineage=lineage,
        )

    return _scored
