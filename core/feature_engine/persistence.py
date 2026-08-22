"""Writing feature vectors to Module 03's `feature_vectors` table.

Included because Module 07 already built `get_feature_vector_as_of` to
*read* this table, which implies something has to write it — and this is
the module that produces them.

Insert-only, matching the pattern established in Module 05: re-running a
computation inserts what is missing and skips what is already there
(`ON CONFLICT DO NOTHING` against Module 03's uniqueness constraint on
`(security_id, feature_schema_version_id, event_time)`). `DO NOTHING`
rather than `DO UPDATE` because a recorded vector is what a signal cited;
overwriting it would make that signal unverifiable.

Only vectors that actually computed something are written. A vector whose
every feature is None records nothing but absence, and persisting it
would put rows in the table that assert a security *had* features on a
date when it had none.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.feature_engine.vector import BatchFeatureResult
from infra.db.schema.versioning import feature_vectors

#: Rows per INSERT.
BATCH_SIZE = 1_000


def write_feature_vectors(connection: Connection, result: BatchFeatureResult) -> int:
    """Persist a batch's vectors. Returns the number of rows inserted.

    Requires `result.feature_schema_version_id` — an unstamped vector has
    no reproducible definition, and writing one would defeat the point of
    versioning the schema at all.
    """
    if result.feature_schema_version_id is None:
        raise ValueError(
            "Cannot persist feature vectors without a feature_schema_version_id: "
            "an unstamped vector is not reproducible. Call "
            "publish_feature_schema_version() first."
        )

    rows: list[dict[str, Any]] = []
    for vector in result.vectors.values():
        available = vector.available_features()
        if not available or vector.event_time is None or vector.availability_time is None:
            continue
        rows.append(
            {
                "security_id": vector.security_id,
                "feature_schema_version_id": result.feature_schema_version_id,
                "event_time": vector.event_time,
                "availability_time": vector.availability_time,
                "features": {
                    **available,
                    # The evidence travels with the vector into storage,
                    # so Module 09 can trace an INSUFFICIENT_EVIDENCE
                    # decision back to what was actually missing.
                    "_evidence": {
                        "bars_available": vector.evidence.bars_available,
                        "bars_required": vector.evidence.bars_required,
                        "coverage_ratio": vector.evidence.coverage_ratio,
                        "has_sufficient_history": vector.evidence.has_sufficient_history,
                        "missing_inputs": {
                            name: reason.value
                            for name, reason in vector.evidence.missing_inputs.items()
                        },
                        "unavailable_features": list(vector.evidence.unavailable_features),
                    },
                },
            }
        )

    inserted = 0
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        if not batch:
            continue
        statement = (
            insert(feature_vectors)
            .values(batch)
            .on_conflict_do_nothing(
                index_elements=["security_id", "feature_schema_version_id", "event_time"]
            )
            .returning(feature_vectors.c.id)
        )
        inserted += len(connection.execute(statement).fetchall())
    return inserted
