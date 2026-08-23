"""Writing signals, immutably, once per scoring.

`signals` is append-only (Module 03's trigger rejects UPDATE and DELETE
with SQLSTATE 23001), and a correction is a new row whose
`supersedes_signal_id` points at the one it replaces. That much the schema
enforces.

Uniqueness is the database's job, as it is for every other result table.
Migration 0005 added a **partial** unique index on the tuple that
identifies one scoring of one candidate — `(security_id, event_time,
data_snapshot_id, scoring_configuration_id)` — restricted to rows with no
`supersedes_signal_id`. `write_signal` inserts with `ON CONFLICT DO
NOTHING` against it, matching Modules 05, 08, 09, 11 and 12.

Partial, because a correction is deliberately a second row carrying the
same identity and pointing at the row it replaces. An unconditional
constraint would make corrections impossible; this one constrains only
the uncorrected originals.

Module 13 originally shipped a read-then-insert guard here, which closed
the ordinary case but was not race-proof — two concurrent writers could
both pass the check before either inserted. That guard is gone; the index
does the work now.

Gated candidates write nothing. See `gating.py` on why an
`INSUFFICIENT_EVIDENCE` row would be a false statement for them.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.scoring.components import COMPONENT_NAMES
from core.scoring.config import stored_probability_definition
from core.scoring.engine import ScoredSignal
from infra.db.schema.intelligence import signals

#: The tuple that identifies one scoring of one candidate, matching the
#: `uq_signals_identity` partial unique index from migration 0005.
IDENTITY_COLUMNS: tuple[str, ...] = (
    "security_id",
    "event_time",
    "data_snapshot_id",
    "scoring_configuration_id",
)

#: The index's predicate, repeated here because `ON CONFLICT` has to name
#: it to infer a partial index.
IDENTITY_PREDICATE = signals.c.supersedes_signal_id.is_(None)


class SignalNotPersistable(ValueError):
    """A gated candidate has no signal row to write."""


def write_signal(
    connection: Connection,
    signal: ScoredSignal,
    *,
    supersedes: UUID | None = None,
) -> UUID | None:
    """Persist one signal. Returns its ID, or None if it already existed.

    `supersedes` records a correction: the new row points at the original,
    which stays exactly as written. The unique index does not apply to it
    — its predicate covers only rows with no `supersedes_signal_id` —
    because superseding is the one legitimate reason for a second row
    carrying the same identity.

    Returns None when the row was already there, which is how a caller
    tells "written" from "already recorded". Both are success.
    """
    if not signal.writes_signal:
        raise SignalNotPersistable(
            f"{signal.decision.value} candidates are gated out of scoring and write "
            "no signal row. Writing one would claim ARGUS could not gather evidence, "
            "when in fact the evidence says the setup is over."
        )

    statement = (
        insert(signals)
        .values(_row(signal, supersedes))
        .on_conflict_do_nothing(
            index_elements=list(IDENTITY_COLUMNS),
            index_where=IDENTITY_PREDICATE,
        )
        .returning(signals.c.id)
    )
    return connection.execute(statement).scalar_one_or_none()


def write_signals(connection: Connection, results: list[ScoredSignal]) -> list[UUID]:
    """Persist every persistable signal in a scan. Gated ones are skipped.

    Returns the IDs actually written — shorter than the input whenever a
    candidate was gated or already recorded, which is information the
    caller needs and a count would hide.
    """
    written: list[UUID] = []
    for signal in results:
        if not signal.writes_signal:
            continue
        signal_id = write_signal(connection, signal)
        if signal_id is not None:
            written.append(signal_id)
    return written


def _row(signal: ScoredSignal, supersedes: UUID | None) -> dict[str, Any]:
    # One column per component, by name. The seven components and the
    # seven `component_*` columns correspond exactly, which is what lets a
    # stored row's breakdown reconstruct its own composite.
    components = {f"component_{name}": signal.component_value(name) for name in COMPONENT_NAMES}
    return {
        "security_id": signal.security_id,
        "event_time": signal.event_time,
        "evidence_status": signal.evidence_status.value,
        "argus_score": signal.argus_score,
        "confidence": signal.confidence,
        "opportunity_score": signal.opportunity_score,
        "risk_score": signal.risk_score,
        # Always NULL, with its definition stored anyway so the structure
        # is present and the absence is unambiguous.
        "probability": signal.probability,
        "probability_definition": stored_probability_definition(),
        "detail": _detail(signal),
        **components,
        **{
            name: getattr(signal.lineage, name)
            for name in (
                "target_model_version_id",
                "feature_schema_version_id",
                "data_snapshot_id",
                "scoring_configuration_id",
                "universe_version_id",
                "detection_configuration_id",
            )
        },
        "supersedes_signal_id": supersedes,
    }


def _detail(signal: ScoredSignal) -> dict[str, Any]:
    """Everything beneath the seven stored component numbers.

    The seven `component_*` columns say *what* each component scored; this
    says why — the raw readings, what each ramp made of them, which
    components could not be measured and for what reason, how much of the
    weight was measurable, and the configuration's calibration status.
    Module 03's comment on the table promises a user can always see why a
    score is what it is; the columns alone do not keep that promise.

    Deliberately not the whole of `as_dict()`: the five numbers and the
    lineage are already columns, and storing them twice would create two
    places for them to disagree.
    """
    return {
        "decision": signal.decision.value,
        "components": {
            name: signal.components[name].as_dict()
            for name in COMPONENT_NAMES
            if name in signal.components
        },
        "confidence_assessment": (
            signal.confidence_assessment.as_dict() if signal.confidence_assessment else None
        ),
        "weight_coverage": signal.weight_coverage,
        "verdict": signal.verdict.as_dict() if signal.verdict else None,
        "probability_status": signal.probability_status,
        "calibration_status": signal.calibration_status,
    }
