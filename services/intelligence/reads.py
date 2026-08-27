"""Reading stored artefacts into the shapes Modules 13/11/12/16 expect.

Every function here loads rows and reshapes them. Nothing computes a
score, derives a state, measures similarity or assesses risk — those all
happened in earlier modules and are stored. If a function here ever needs
a number that is not in a column, the work belongs upstream.

## Why the reshaping exists at all

Module 16's narrators take plain dicts — `signal_facts` documents that it
accepts "`ScoredSignal.as_dict()` **or a `signals` row**". A stored row is
almost that shape already; what differs is that `decision`,
`weight_coverage` and the per-component detail live inside the `detail`
JSONB rather than at the top level, because Module 03 made the seven
component *values* columns and left the reasoning in JSONB.

So `signal_as_dict` flattens `detail` up. That is a shape change, not a
computation: every value comes across untouched, and a test asserts the
stored numbers and the served numbers are identical.

## Latest, not all

A security accumulates one signal per scan date. This module answers "what
does ARGUS think **now**", so every read here takes the most recent row
and says when it was computed. History is Module 17's question.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.engine import Connection

from core.scoring.components import COMPONENT_NAMES
from infra.db.schema.intelligence import (
    historical_similarity_results,
    market_state,
    market_state_transitions,
    pending_material_events,
    signals,
)

__all__ = [
    "latest_signal",
    "latest_similarity",
    "pending_events",
    "signal_as_dict",
    "state_row",
    "transitions_for",
]


def latest_signal(connection: Connection, security_id: UUID) -> dict[str, Any] | None:
    """The most recent signal ARGUS still stands behind, flattened.

    A correction is a new row whose `supersedes_signal_id` points at the
    row it replaces (migration 0005). So the row to exclude is the one
    being *pointed at*, not the one doing the pointing — the correction
    is the current answer and the original is the withdrawn one.

    Getting this backwards is easy and quiet: `signals` is append-only, so
    a withdrawn score is still in the table and still perfectly readable,
    and serving it would show a retracted number with full provenance
    attached — which makes it look more trustworthy rather than less. An
    integration test supersedes a score and asserts which of the two comes
    back.
    """
    superseded = select(signals.c.supersedes_signal_id).where(
        signals.c.security_id == security_id,
        signals.c.supersedes_signal_id.is_not(None),
    )
    row = connection.execute(
        select(signals)
        .where(
            signals.c.security_id == security_id,
            # `NOT IN` over a set containing NULL matches nothing at all,
            # which would empty this result silently. The subquery is
            # filtered to non-null for that reason.
            signals.c.id.not_in(superseded),
        )
        .order_by(desc(signals.c.event_time), desc(signals.c.created_at))
        .limit(1)
    ).one_or_none()
    return signal_as_dict(row) if row is not None else None


def signal_as_dict(row: Any) -> dict[str, Any]:
    """A `signals` row in the shape Module 16's narrators read.

    `detail` is flattened up rather than nested, because Module 03 put the
    seven component values in columns and the reasoning in JSONB, and a
    narrator wants both at one level. Nothing is recomputed: the component
    values come from the columns, the per-component reasoning from
    `detail`, and where both exist they are the same number by
    construction.
    """
    detail = dict(row.detail or {})
    components = dict(detail.get("components") or {})

    # The seven columns are authoritative for the value; `detail` carries
    # each component's inputs, ramps and unavailability. Merged so a
    # consumer sees one object per component rather than two halves.
    for name in COMPONENT_NAMES:
        column_value = getattr(row, f"component_{name}", None)
        entry = dict(components.get(name) or {})
        entry.setdefault("name", name)
        entry["value"] = float(column_value) if column_value is not None else None
        components[name] = entry

    return {
        "signal_id": str(row.id),
        "security_id": str(row.security_id),
        "event_time": row.event_time,
        "evidence_status": row.evidence_status,
        "decision": detail.get("decision"),
        "argus_score": _float(row.argus_score),
        "confidence": _float(row.confidence),
        "opportunity_score": _float(row.opportunity_score),
        "risk_score": _float(row.risk_score),
        "probability": _float(row.probability),
        "probability_definition": row.probability_definition,
        "probability_status": detail.get("probability_status"),
        "components": components,
        "confidence_assessment": detail.get("confidence_assessment"),
        "weight_coverage": detail.get("weight_coverage"),
        "verdict": detail.get("verdict"),
        "calibration_status": detail.get("calibration_status"),
        "target_model_version_id": str(row.target_model_version_id),
        "scoring_configuration_id": str(row.scoring_configuration_id),
        "feature_schema_version_id": str(row.feature_schema_version_id),
        "data_snapshot_id": str(row.data_snapshot_id),
        "universe_version_id": str(row.universe_version_id),
        "detection_configuration_id": str(row.detection_configuration_id),
        "created_at": row.created_at,
    }


def latest_similarity(connection: Connection, security_id: UUID) -> dict[str, dict[str, Any]]:
    """The most recent stored result per scope, keyed by scope.

    Per scope rather than one row: Module 11 stores cross-asset and
    same-asset as **separate rows** precisely so they cannot be blended,
    and reading them into one object would be the first step towards
    doing it anyway.
    """
    rows = connection.execute(
        select(historical_similarity_results)
        .where(historical_similarity_results.c.security_id == security_id)
        .order_by(
            desc(historical_similarity_results.c.event_time),
            desc(historical_similarity_results.c.computed_at),
        )
    ).all()

    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        scope = str(row.scope)
        if scope in latest:
            continue
        latest[scope] = {
            "scope": scope,
            "match_count": int(row.similar_setup_count),
            "median_outcome": _float(row.median_outcome),
            "average_outcome": _float(row.average_outcome),
            "failure_rate": _float(row.failure_rate),
            "similarity_distribution": row.similarity_distribution,
            "mfe_distribution": row.mfe_distribution,
            "mae_distribution": row.mae_distribution,
            "expansion_magnitude": row.expansion_magnitude,
            "time_to_expansion": row.time_to_expansion,
            "outcome_by_regime": row.outcome_by_regime,
            "computed_at": row.computed_at,
        }
    return latest


def state_row(connection: Connection, security_id: UUID) -> Any | None:
    """This security's row in Module 10's state projection.

    The projection, not the transition log. Module 10's rule is that the
    log is authoritative and the projection is rebuildable from it; for
    "what state is this in right now" the projection is the answer, and
    a test asserts the two agree.
    """
    return connection.execute(
        select(market_state).where(market_state.c.security_id == security_id)
    ).one_or_none()


def transitions_for(
    connection: Connection, security_id: UUID, *, since: datetime | None = None
) -> list[Any]:
    """Every recorded state change, oldest first. The chart's marks."""
    query = select(market_state_transitions).where(
        market_state_transitions.c.security_id == security_id
    )
    if since is not None:
        query = query.where(market_state_transitions.c.transition_time >= since)
    return list(connection.execute(query.order_by(market_state_transitions.c.transition_time)))


def pending_events(
    connection: Connection, security_id: UUID, *, as_of: datetime
) -> list[dict[str, Any]]:
    """Scheduled events knowable at `as_of` and still ahead of it.

    PIT-filtered on `availability_time` like every canonical read in
    ARGUS: an earnings date announced next week must not appear in
    today's answer.
    """
    rows = connection.execute(
        select(pending_material_events)
        .where(
            pending_material_events.c.security_id == security_id,
            pending_material_events.c.availability_time <= as_of,
            pending_material_events.c.scheduled_for >= as_of,
        )
        .order_by(pending_material_events.c.scheduled_for)
    ).all()
    return [
        {
            "event_type": row.event_type,
            "scheduled_for": row.scheduled_for,
            "is_binary": bool(row.is_binary),
            "source": row.source,
        }
        for row in rows
    ]


def _float(value: Any) -> float | None:
    """Postgres `Numeric` arrives as `Decimal`. None stays None.

    None rather than 0.0, always — the rule eighteen modules have
    enforced, applied at the last place a number leaves the database.
    """
    return None if value is None else float(value)
