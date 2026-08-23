"""Database fixtures for Module 15.

The classification logic is pure and is tested without a database in
`tests/unit/outcome_tracking/`. What needs a real PostgreSQL is the part
that can leak: excursions are measured from bars and corporate actions
loaded through Module 07's PIT filters, and a mock would happily return
whatever the leakage test expected it to.

Setups are built through Module 14's own functions rather than by
inserting rows, so the case assembly is exercised against the event
histories that module actually writes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.lifecycle.derivation import advance
from core.lifecycle.engine import open_setup
from core.lifecycle.events import ACTIVATED, ENDPOINT_REACHED, QUALIFIED
from core.outcome_tracking.config import OutcomeConfig, publish_outcome_snapshot
from data.canonical_model.pit import session_close
from data.canonical_model.records import CanonicalTimeframe
from infra.db.enums import SetupLifecycleStatus
from infra.db.schema.canonical import canonical_corporate_actions, canonical_ohlcv
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

#: The bars start here; everything else is expressed as an offset.
FIRST_BAR = datetime(2024, 1, 2, tzinfo=UTC)
#: When outcomes are computed. Comfortably after every window used here.
AS_OF = datetime(2024, 9, 2, 21, 0, tzinfo=UTC)


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    """A connection rolled back after each test.

    Rollback rather than DELETE: `canonical_ohlcv`, `setup_events` and
    `setup_outcomes` are all guarded, so a delete-based teardown would be
    refused by Module 03.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def snapshot_id(connection: Connection) -> UUID:
    """The snapshot every outcome cites: PIT cutoff plus success criterion."""
    return publish_outcome_snapshot(connection, OutcomeConfig(), as_of=AS_OF)


def write_bars(
    connection: Connection,
    security_id: UUID,
    closes: list[float],
    *,
    start: datetime = FIRST_BAR,
    volume: int = 1_000_000,
    availability_lag: timedelta = timedelta(hours=1),
) -> list[datetime]:
    """Consecutive business-day bars. Highs and lows straddle the close.

    The straddle is deliberately narrow so a test that moves a close moves
    MFE predictably, and a test that revises a single high can be seen
    against an otherwise smooth series.
    """
    dates = pd.bdate_range(start, periods=len(closes), tz="UTC")
    stamps: list[datetime] = []
    for day, close in zip(dates, closes, strict=True):
        event_time = session_close(day.date())
        price = Decimal(str(round(close, 4)))
        connection.execute(
            canonical_ohlcv.insert().values(
                security_id=security_id,
                timeframe=CanonicalTimeframe.DAILY.value,
                event_time=event_time,
                observation_time=event_time + availability_lag,
                availability_time=event_time + availability_lag,
                ingestion_time=event_time + availability_lag,
                open_raw=price,
                high_raw=price * Decimal("1.005"),
                low_raw=price * Decimal("0.995"),
                close_raw=price,
                volume_raw=volume,
            )
        )
        stamps.append(event_time)
    return stamps


def revise_bar(
    connection: Connection,
    security_id: UUID,
    *,
    event_time: datetime,
    high: float,
    available_at: datetime,
) -> None:
    """File a revision of one bar, knowable only from `available_at`.

    A new row, never an edit — `canonical_ohlcv` is append-only, and
    Module 05's rule is that a restatement arrives as a new row with a
    later observation time. Which is exactly what makes the leak
    constructible: the revised bar genuinely exists, and only the
    availability filter keeps it out of an earlier query.
    """
    price = Decimal(str(round(high, 4)))
    connection.execute(
        canonical_ohlcv.insert().values(
            security_id=security_id,
            timeframe=CanonicalTimeframe.DAILY.value,
            event_time=event_time,
            observation_time=available_at,
            availability_time=available_at,
            ingestion_time=available_at,
            open_raw=price,
            high_raw=price,
            low_raw=price * Decimal("0.995"),
            close_raw=price,
            volume_raw=1_000_000,
        )
    )


def write_split(
    connection: Connection,
    security_id: UUID,
    *,
    effective_date: datetime,
    available_at: datetime,
    numerator: int = 2,
    denominator: int = 1,
) -> None:
    """A split with its own availability time.

    Splitting the effective date from the availability time is the whole
    point of the fixture: a provider commonly backfills an action days
    after it took effect, and a series adjusted for an action ARGUS did
    not yet know about is a leak that produces a plausible number rather
    than an error.
    """
    connection.execute(
        canonical_corporate_actions.insert().values(
            security_id=security_id,
            action_type="SPLIT",
            effective_date=effective_date,
            event_time=effective_date,
            observation_time=available_at,
            availability_time=available_at,
            ingestion_time=available_at,
            details={"numerator": numerator, "denominator": denominator},
        )
    )


@pytest.fixture
def concluded_setup(connection: Connection, lineage) -> Callable[..., UUID]:  # noqa: F811
    """Build a setup and drive it to a terminal event through Module 14.

    Uses the real lifecycle functions rather than inserting event rows, so
    the case assembly is tested against histories Module 14 actually
    produces — including the `status_before` payload this module reads.
    """

    def _build(
        security_id: UUID,
        *,
        detected_at: datetime,
        activated_at: datetime | None,
        terminal_at: datetime,
        terminal_event: str = ENDPOINT_REACHED,
        market_state: str = "UPTREND",
        retreats: tuple[datetime, ...] = (),
    ) -> UUID:
        setup_id, _ = open_setup(connection, security_id, as_of=detected_at, lineage=lineage)
        if activated_at is not None:
            advance(
                connection,
                setup_id,
                to=SetupLifecycleStatus.QUALIFICATION,
                event_type=QUALIFIED,
                occurred_at=detected_at + timedelta(days=1),
                payload={"market_state": "CONSOLIDATION"},
            )
            advance(
                connection,
                setup_id,
                to=SetupLifecycleStatus.ACTIVE,
                event_type=ACTIVATED,
                occurred_at=activated_at,
                payload={"market_state": "BREAKOUT_READY"},
            )
            for moment in retreats:
                advance(
                    connection,
                    setup_id,
                    to=SetupLifecycleStatus.ACTIVE,
                    event_type="market_state_retreat",
                    occurred_at=moment,
                    payload={"market_state": "CONSOLIDATION", "backward_transitions": 1},
                )
        advance(
            connection,
            setup_id,
            to=SetupLifecycleStatus.OUTCOME,
            event_type=terminal_event,
            occurred_at=terminal_at,
            payload={
                "market_state": market_state,
                "status_before": (
                    SetupLifecycleStatus.ACTIVE.value
                    if activated_at is not None
                    else SetupLifecycleStatus.DETECTION.value
                ),
            },
        )
        return setup_id

    return _build
