"""Writing similarity evidence to `historical_similarity_results`.

**Two rows per query, always** — one `CROSS_ASSET`, one `SAME_ASSET`.
Module 03's unique constraint is on
`(security_id, event_time, scope, data_snapshot_id)`, which is the schema
enforcing the same separation this module enforces in its types: the two
scopes cannot occupy one row even if someone wanted them to.

A scope with no analogues still gets a row, carrying `similar_setup_count
= 0` and its sufficiency. "We searched and found nothing" and "we never
searched" are different facts, and only the first leaves a row.

Insert-only with `ON CONFLICT DO NOTHING`, matching Modules 05, 08 and 09:
a recorded result is what a downstream decision cited, and rewriting it
would make that decision unverifiable.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.historical_similarity.engine import ScopedEvidence, SimilarityEvidence
from infra.db.schema.intelligence import historical_similarity_results


def write_similarity_results(connection: Connection, evidence: SimilarityEvidence) -> int:
    """Persist both scopes. Returns rows inserted.

    Requires `data_snapshot_id`: the column is `NOT NULL`, and for the
    reason Module 03 gives on `data_snapshot` — re-running against the
    snapshot must see exactly the rows knowable at its cutoff. Without it
    a stored result could not be reproduced, and these thresholds are
    placeholders that will change.
    """
    if evidence.data_snapshot_id is None:
        raise ValueError(
            "Cannot persist similarity results without a data_snapshot_id: the "
            "distance radius is an unvalidated placeholder and the result must "
            "stay attributable to it. Call publish_similarity_configuration() first."
        )

    rows = [_row(evidence, scoped) for scoped in (evidence.cross_asset, evidence.same_asset)]
    statement = (
        insert(historical_similarity_results)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["security_id", "event_time", "scope", "data_snapshot_id"]
        )
        .returning(historical_similarity_results.c.id)
    )
    return len(connection.execute(statement).fetchall())


def _row(evidence: SimilarityEvidence, scoped: ScopedEvidence) -> dict[str, Any]:
    statistics = scoped.statistics
    return {
        "security_id": evidence.security_id,
        "event_time": evidence.as_of,
        "scope": scoped.scope.value,
        "similar_setup_count": scoped.count,
        "similarity_distribution": {
            "distances": scoped.distance_distribution(),
            "considered": scoped.considered,
            "incomparable": scoped.incomparable,
            "sufficiency": statistics.sufficiency.value,
            # Carried onto every row so a stored number can never be read
            # as though its radius had been validated.
            **evidence.metric_metadata,
            # Same-asset rows carry the transition facts too; cross-asset
            # rows do not, since they are a property of this security.
            **(
                {"same_asset_history": evidence.same_asset_history.as_dict()}
                if scoped.scope.value == "SAME_ASSET"
                else {}
            ),
        },
        "median_outcome": statistics.median_outcome,
        "average_outcome": statistics.average_outcome,
        "mfe_distribution": statistics.mfe.as_dict() if statistics.mfe else None,
        "mae_distribution": statistics.mae.as_dict() if statistics.mae else None,
        "failure_rate": statistics.failure_rate,
        "expansion_magnitude": (
            statistics.expansion_magnitude.as_dict() if statistics.expansion_magnitude else None
        ),
        "time_to_expansion": (
            statistics.time_to_expansion.as_dict() if statistics.time_to_expansion else None
        ),
        "outcome_by_regime": statistics.outcome_by_regime,
        "feature_schema_version_id": evidence.feature_schema_version_id,
        "data_snapshot_id": evidence.data_snapshot_id,
        "computed_at": evidence.as_of,
    }
