"""`get_as_of` — the single point every future module reads PIT data through.

Everything above this file (`ohlcv.py`, `fundamentals.py`, ...) is a
typed, entity-specific function a caller can use directly. This file adds
one more thing on top: a single dispatcher matching the shape Module 07
was asked to build, so there is exactly one name — `get_as_of` — that
"read data as of a date" means in this codebase. A future module reaching
for raw SQL against `canonical_ohlcv` or `canonical_fundamentals` instead
of this function is the one thing this module exists to make unnecessary.

**Dual-mode by construction, not by special-casing.** Every call takes an
explicit `as_of: datetime` — there is no "give me current data" branch,
no default of "now". A live-mode caller passes `datetime.now(UTC)`; a
Module 17 batch-replay caller passes a date from 2015. Both go through the
identical code path with an identical return type, which is exactly the
property `docs/architecture/CROSS_CUTTING_REQUIREMENTS.md` asks Modules
08-16 to have. If this function needed a `live: bool` flag or a separate
`get_as_of_batch`, that requirement would already be broken here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Connection

from core.data_validation.corporate_actions import (
    CorporateActionAsOf,
    get_corporate_actions_as_of,
)
from core.data_validation.entities import EntityType
from core.data_validation.feature_vectors import FeatureVectorAsOf, get_feature_vector_as_of
from core.data_validation.fundamentals import (
    FundamentalAsOf,
    get_fundamental_as_of,
    get_latest_fundamental_as_of,
)
from core.data_validation.ohlcv import OhlcvBarAsOf, get_ohlcv_bar_as_of
from core.data_validation.result import AsOfResult
from core.data_validation.universe import MembershipAsOf, get_universe_membership_as_of
from data.canonical_model.records import CanonicalStatementType, CanonicalTimeframe


def get_as_of(
    connection: Connection,
    entity_type: EntityType,
    security_id: UUID,
    as_of: datetime,
    **params: Any,
) -> AsOfResult[
    OhlcvBarAsOf | FundamentalAsOf | list[CorporateActionAsOf] | MembershipAsOf | FeatureVectorAsOf
]:
    """Read one entity's data as it was knowable on `as_of`.

    Every entity filters on `availability_time` (or, for universe
    membership, the equivalent listing-interval predicate) — never on
    `event_time`. Required keyword params by `entity_type`:

    - `OHLCV`: `bar_date` (date). Optional: `timeframe` (default DAILY).
    - `FUNDAMENTALS`: either `fiscal_period_end` (date) for one specific
      period, or `statement_type` alone for the latest known period as of
      `as_of`. `statement_type` (CanonicalStatementType) is always
      required.
    - `CORPORATE_ACTIONS`: none. Returns every known action as a list —
      an empty list is a legitimate answer, not a miss.
    - `UNIVERSE_MEMBERSHIP`: `universe_version_id` (UUID).
    - `FEATURE_VECTOR`: `feature_schema_version_id` (UUID), `event_time`
      (datetime).

    Raises `ValueError` immediately on a missing required param, rather
    than silently querying the wrong thing.
    """
    if entity_type is EntityType.OHLCV:
        bar_date = _require(params, "bar_date", entity_type)
        timeframe = params.get("timeframe", CanonicalTimeframe.DAILY)
        return get_ohlcv_bar_as_of(connection, security_id, bar_date, as_of, timeframe=timeframe)

    if entity_type is EntityType.FUNDAMENTALS:
        statement_type = _require(params, "statement_type", entity_type)
        fiscal_period_end = params.get("fiscal_period_end")
        if fiscal_period_end is not None:
            return get_fundamental_as_of(
                connection, security_id, statement_type, fiscal_period_end, as_of
            )
        return get_latest_fundamental_as_of(connection, security_id, statement_type, as_of)

    if entity_type is EntityType.CORPORATE_ACTIONS:
        actions = get_corporate_actions_as_of(connection, security_id, as_of)
        return AsOfResult.hit(actions, as_of=as_of)

    if entity_type is EntityType.UNIVERSE_MEMBERSHIP:
        universe_version_id = _require(params, "universe_version_id", entity_type)
        return get_universe_membership_as_of(connection, universe_version_id, security_id, as_of)

    if entity_type is EntityType.FEATURE_VECTOR:
        feature_schema_version_id = _require(params, "feature_schema_version_id", entity_type)
        event_time = _require(params, "event_time", entity_type)
        return get_feature_vector_as_of(
            connection, security_id, feature_schema_version_id, event_time, as_of
        )

    raise ValueError(f"Unhandled entity type: {entity_type!r}")


def _require(params: dict[str, Any], name: str, entity_type: EntityType) -> Any:
    try:
        return params[name]
    except KeyError:
        raise ValueError(f"{entity_type.value} requires the {name!r} parameter.") from None


__all__ = ["CanonicalStatementType", "EntityType", "get_as_of"]
