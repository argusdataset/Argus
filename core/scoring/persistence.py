"""Writing signals, immutably, with a guard the schema does not provide.

`signals` is append-only (Module 03's trigger rejects UPDATE and DELETE
with SQLSTATE 23001), and a correction is a new row whose
`supersedes_signal_id` points at the one it replaces. That much the schema
enforces.

What the schema does **not** have is a unique constraint. Every other
result table in ARGUS has one — `feature_vectors`,
`eligibility_check_results`, `historical_similarity_results`,
`pending_material_events` — and each of those uses it with `ON CONFLICT DO
NOTHING` so a re-run inserts what is missing and rewrites nothing. Here,
re-running the same scan would silently produce a second identical row,
and since the rows are immutable there would then be no way to tell which
one a downstream decision cited.

So `write_signal` checks before inserting on the tuple that identifies one
scoring of one candidate — `(security_id, event_time, data_snapshot_id,
scoring_configuration_id)`. This is a read-then-insert guard rather than a
database constraint, which means it is not race-proof: two concurrent
writers can both pass the check. That is a real limitation and the right
fix is a migration adding the constraint, which is Module 03's call and
is flagged in the module README rather than made here.

Gated candidates write nothing. See `gating.py` on why an
`INSUFFICIENT_EVIDENCE` row would be a false statement for them.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection

from core.scoring.components import COMPONENT_NAMES
from core.scoring.config import stored_probability_definition
from core.scoring.engine import ScoredSignal
from infra.db.schema.intelligence import signals

#: The tuple that identifies one scoring of one candidate. Not a database
#: constraint — see the module docstring.
IDENTITY_COLUMNS: tuple[str, ...] = (
    "security_id",
    "event_time",
    "data_snapshot_id",
    "scoring_configuration_id",
)


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
    which stays exactly as written. A correction is always a new row, so
    the identity guard is deliberately not applied to it — superseding is
    the one legitimate reason for a second row with the same identity.
    """
    if not signal.writes_signal:
        raise SignalNotPersistable(
            f"{signal.decision.value} candidates are gated out of scoring and write "
            "no signal row. Writing one would claim ARGUS could not gather evidence, "
            "when in fact the evidence says the setup is over."
        )

    if supersedes is None:
        existing = _existing_id(connection, signal)
        if existing is not None:
            return None

    return connection.execute(
        signals.insert().values(_row(signal, supersedes)).returning(signals.c.id)
    ).scalar_one()


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


def _existing_id(connection: Connection, signal: ScoredSignal) -> UUID | None:
    row = _row(signal, None)
    return connection.execute(
        select(signals.c.id)
        .where(
            and_(
                *(signals.c[column] == row[column] for column in IDENTITY_COLUMNS),
                signals.c.supersedes_signal_id.is_(None),
            )
        )
        .limit(1)
    ).scalar_one_or_none()


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
