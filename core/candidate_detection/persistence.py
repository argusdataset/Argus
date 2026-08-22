"""Recording gate results in Module 03's `eligibility_check_results`.

One row per (run, security, gate) — the shape Module 03's unique
constraint already specifies. Six rows per candidate, including the gates
that passed.

**Passes are stored, not just failures.** Storing only rejections would
make "this gate was evaluated and passed" indistinguishable from "this
gate was never run", which is precisely the distinction Module 09 exists
to preserve at the candidate level, and it would be perverse to lose it
at the gate level. It also makes the diagnosis the prompt asks for
possible: comparing pass rates per gate across runs is how you notice a
gate that has quietly stopped rejecting anything.

Insert-only with `ON CONFLICT DO NOTHING`, matching Modules 05 and 08: a
re-run inserts what is missing and never overwrites. A recorded gate
verdict is what a downstream `INSUFFICIENT_EVIDENCE` decision cited, and
rewriting it would make that decision unverifiable after the fact.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.candidate_detection.eligibility.gates import EligibilityReport
from infra.db.schema.intelligence import eligibility_check_results

#: Rows per INSERT.
BATCH_SIZE = 1_000


def write_eligibility_results(connection: Connection, report: EligibilityReport) -> int:
    """Persist every gate result in `report`. Returns rows inserted.

    Requires `detection_configuration_id`: the column is `NOT NULL` in
    Module 03's schema, and for the same reason Module 08 refuses to write
    an unstamped feature vector — a gate verdict whose configuration
    cannot be recovered is not reproducible, and an unreproducible
    rejection is indistinguishable from an arbitrary one.
    """
    if report.detection_configuration_id is None:
        raise ValueError(
            "Cannot persist eligibility results without a detection_configuration_id: "
            "an unstamped gate verdict is not reproducible. Call "
            "publish_detection_configuration() first."
        )

    rows = [
        {
            "run_id": report.run_id,
            "security_id": security_id,
            "gate": result.gate.value,
            "passed": result.passed,
            "detail": result.detail,
            "detection_configuration_id": report.detection_configuration_id,
            "evaluated_at": report.as_of,
        }
        for security_id, outcome in report.outcomes.items()
        for result in outcome.results.values()
    ]

    inserted = 0
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        if not batch:
            continue
        statement = (
            insert(eligibility_check_results)
            .values(batch)
            .on_conflict_do_nothing(index_elements=["run_id", "security_id", "gate"])
            .returning(eligibility_check_results.c.id)
        )
        inserted += len(connection.execute(statement).fetchall())
    return inserted
