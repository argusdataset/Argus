"""PIT-safe access to feature vectors.

No feature computation exists yet (Module 08). This exists now because
`feature_vectors` is already in Module 03's schema, and its
`availability_time` is defined there as the max across every input that
fed the vector — meaning a vector's PIT-correctness is checkable without
re-deriving its input set, *if* the query layer respects that column
rather than recomputing anything. This is that respect: the same
enforcement primitive as every other entity, nothing input-aware added.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Connection

from core.data_validation.engine import select_latest_as_of
from core.data_validation.result import AsOfResult, MissReason
from infra.db.schema.versioning import feature_vectors


@dataclass(frozen=True, slots=True)
class FeatureVectorAsOf:
    """One feature vector, as it was known at query time."""

    security_id: UUID
    feature_schema_version_id: UUID
    event_time: datetime
    availability_time: datetime
    features: dict[str, Any]


def get_feature_vector_as_of(
    connection: Connection,
    security_id: UUID,
    feature_schema_version_id: UUID,
    event_time: datetime,
    as_of: datetime,
) -> AsOfResult[FeatureVectorAsOf]:
    """The feature vector for one (security, schema version, event), as
    knowable at `as_of`.
    """
    row = select_latest_as_of(
        connection,
        feature_vectors,
        key={
            "security_id": security_id,
            "feature_schema_version_id": feature_schema_version_id,
            "event_time": event_time,
        },
        as_of=as_of,
    )
    if row is None:
        return AsOfResult.miss(MissReason.NOT_YET_AVAILABLE, as_of=as_of)

    return AsOfResult.hit(
        FeatureVectorAsOf(
            security_id=row.security_id,
            feature_schema_version_id=row.feature_schema_version_id,
            event_time=row.event_time,
            availability_time=row.availability_time,
            features=dict(row.features),
        ),
        as_of=as_of,
    )
