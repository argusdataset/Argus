"""Corporate actions, feature vectors, and the get_as_of dispatcher's
remaining entity types — the ones not already covered by the adversarial
leakage tests or the universe tests.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import Connection

from core.data_validation.corporate_actions import get_corporate_actions_as_of
from core.data_validation.entities import EntityType
from core.data_validation.feature_vectors import get_feature_vector_as_of
from core.data_validation.ohlcv import get_ohlcv_bar_as_of
from core.data_validation.query import get_as_of
from core.data_validation.result import MissReason
from data.canonical_model.pit import session_close
from data.canonical_model.records import CanonicalCorporateActionType, CanonicalTimeframe
from infra.db.schema.canonical import canonical_corporate_actions, canonical_ohlcv
from infra.db.schema.versioning import feature_schema_version, feature_vectors

# --------------------------------------------------------------------------
# Corporate actions
# --------------------------------------------------------------------------


def _insert_action(
    connection: Connection,
    security_id: UUID,
    *,
    effective_date: date,
    availability_time: datetime,
    action_type: CanonicalCorporateActionType = CanonicalCorporateActionType.SPLIT,
) -> None:
    effective = datetime.combine(effective_date, datetime.min.time(), tzinfo=UTC)
    connection.execute(
        canonical_corporate_actions.insert().values(
            security_id=security_id,
            action_type=action_type.value,
            effective_date=effective,
            event_time=effective,
            observation_time=availability_time,
            availability_time=availability_time,
            ingestion_time=availability_time,
            details={"numerator": 4, "denominator": 1},
        )
    )


def test_corporate_actions_as_of_excludes_not_yet_knowable_splits(
    connection: Connection, security_id: UUID
):
    """PIT-correct adjustment must not apply a split before it was knowable."""
    _insert_action(
        connection,
        security_id,
        effective_date=date(2020, 8, 31),
        availability_time=datetime(2020, 8, 31, tzinfo=UTC),
    )
    _insert_action(
        connection,
        security_id,
        effective_date=date(2025, 6, 9),
        availability_time=datetime(2025, 6, 9, tzinfo=UTC),
    )

    before_second_split = get_corporate_actions_as_of(
        connection, security_id, datetime(2021, 1, 1, tzinfo=UTC)
    )
    assert len(before_second_split) == 1
    assert before_second_split[0].effective_date == date(2020, 8, 31)


def test_corporate_actions_as_of_returns_empty_list_not_a_miss(
    connection: Connection, security_id: UUID
):
    """No actions knowable yet is a legitimate answer for a multi-row entity."""
    actions = get_corporate_actions_as_of(connection, security_id, datetime(2000, 1, 1, tzinfo=UTC))
    assert actions == []


def test_corporate_actions_as_of_orders_by_effective_date(
    connection: Connection, security_id: UUID
):
    _insert_action(
        connection,
        security_id,
        effective_date=date(2025, 1, 1),
        availability_time=datetime(2025, 1, 1, tzinfo=UTC),
    )
    _insert_action(
        connection,
        security_id,
        effective_date=date(2020, 1, 1),
        availability_time=datetime(2020, 1, 1, tzinfo=UTC),
    )

    actions = get_corporate_actions_as_of(connection, security_id, datetime(2026, 1, 1, tzinfo=UTC))
    assert [action.effective_date for action in actions] == [date(2020, 1, 1), date(2025, 1, 1)]


def test_get_as_of_dispatcher_handles_corporate_actions(connection: Connection, security_id: UUID):
    _insert_action(
        connection,
        security_id,
        effective_date=date(2020, 8, 31),
        availability_time=datetime(2020, 8, 31, tzinfo=UTC),
    )

    result = get_as_of(
        connection, EntityType.CORPORATE_ACTIONS, security_id, datetime(2021, 1, 1, tzinfo=UTC)
    )
    assert result.found  # a hit, even though the value is a (possibly empty) list
    assert len(result.unwrap()) == 1


# --------------------------------------------------------------------------
# Feature vectors
# --------------------------------------------------------------------------


@pytest.fixture
def schema_version_id(connection: Connection) -> UUID:
    return connection.execute(
        feature_schema_version.insert()
        .values(
            version_label=f"test-schema-{uuid4().hex[:8]}",
            definition={},
            content_checksum="sha",
        )
        .returning(feature_schema_version.c.id)
    ).scalar_one()


def test_feature_vector_as_of_respects_availability(
    connection: Connection, security_id: UUID, schema_version_id: UUID
):
    event_time = datetime(2024, 1, 3, tzinfo=UTC)
    availability_time = datetime(2024, 1, 4, tzinfo=UTC)
    connection.execute(
        feature_vectors.insert().values(
            security_id=security_id,
            feature_schema_version_id=schema_version_id,
            event_time=event_time,
            availability_time=availability_time,
            features={"volatility_compression": 0.42},
        )
    )

    before = get_feature_vector_as_of(
        connection, security_id, schema_version_id, event_time, datetime(2024, 1, 3, 12, tzinfo=UTC)
    )
    after = get_feature_vector_as_of(
        connection, security_id, schema_version_id, event_time, datetime(2024, 1, 5, tzinfo=UTC)
    )

    assert not before
    assert before.reason is MissReason.NOT_YET_AVAILABLE
    assert after.unwrap().features["volatility_compression"] == 0.42


def test_get_as_of_dispatcher_handles_feature_vectors(
    connection: Connection, security_id: UUID, schema_version_id: UUID
):
    event_time = datetime(2024, 1, 3, tzinfo=UTC)
    connection.execute(
        feature_vectors.insert().values(
            security_id=security_id,
            feature_schema_version_id=schema_version_id,
            event_time=event_time,
            availability_time=event_time,
            features={"x": 1},
        )
    )

    result = get_as_of(
        connection,
        EntityType.FEATURE_VECTOR,
        security_id,
        datetime(2024, 1, 4, tzinfo=UTC),
        feature_schema_version_id=schema_version_id,
        event_time=event_time,
    )
    assert result.unwrap().features["x"] == 1


# --------------------------------------------------------------------------
# OHLCV via the dispatcher, and required-parameter enforcement
# --------------------------------------------------------------------------


def test_get_as_of_dispatcher_handles_ohlcv(connection: Connection, security_id: UUID):
    bar_date = date(2024, 1, 3)
    close_time = session_close(bar_date)
    price = Decimal("184.25")
    connection.execute(
        canonical_ohlcv.insert().values(
            security_id=security_id,
            timeframe=CanonicalTimeframe.DAILY.value,
            event_time=close_time,
            observation_time=close_time,
            availability_time=close_time,
            ingestion_time=close_time,
            open_raw=price,
            high_raw=price,
            low_raw=price,
            close_raw=price,
            volume_raw=1000,
        )
    )

    result = get_as_of(
        connection,
        EntityType.OHLCV,
        security_id,
        datetime(2024, 1, 4, tzinfo=UTC),
        bar_date=bar_date,
    )
    assert result.unwrap().close_raw == price

    # Sanity check against the typed function directly too.
    direct = get_ohlcv_bar_as_of(
        connection, security_id, bar_date, datetime(2024, 1, 4, tzinfo=UTC)
    )
    assert direct.unwrap().close_raw == price


@pytest.mark.parametrize(
    ("entity_type", "missing_param"),
    [
        (EntityType.OHLCV, "bar_date"),
        (EntityType.FUNDAMENTALS, "statement_type"),
        (EntityType.UNIVERSE_MEMBERSHIP, "universe_version_id"),
        (EntityType.FEATURE_VECTOR, "feature_schema_version_id"),
    ],
)
def test_get_as_of_raises_immediately_on_a_missing_required_param(
    connection: Connection, security_id: UUID, entity_type, missing_param
):
    """Fail loudly rather than silently querying the wrong thing."""
    with pytest.raises(ValueError, match=missing_param):
        get_as_of(connection, entity_type, security_id, datetime(2024, 1, 1, tzinfo=UTC))


# --------------------------------------------------------------------------
# Dual-mode symmetry: live and batch calls are the same code path
# --------------------------------------------------------------------------


def test_a_live_style_call_and_a_batch_style_call_use_the_identical_function(
    connection: Connection, security_id: UUID
):
    """The property CROSS_CUTTING_REQUIREMENTS.md asks Modules 08-16 for.

    Nothing distinguishes "as_of=now" from "as_of=some date in 2015" other
    than the value passed — same function, same return type, same
    enforcement. This test exists to make that an assertion, not a claim.
    """
    _insert_action(
        connection,
        security_id,
        effective_date=date(2015, 6, 1),
        availability_time=datetime(2015, 6, 1, tzinfo=UTC),
    )

    live_style = get_as_of(connection, EntityType.CORPORATE_ACTIONS, security_id, datetime.now(UTC))
    batch_style = get_as_of(
        connection, EntityType.CORPORATE_ACTIONS, security_id, datetime(2020, 1, 1, tzinfo=UTC)
    )

    assert type(live_style) is type(batch_style)
    assert live_style.unwrap()[0].effective_date == batch_style.unwrap()[0].effective_date
