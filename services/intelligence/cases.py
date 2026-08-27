"""Why did this fail — Module 15's record, narrated by Module 16, passed through.

## Nothing here explains anything

Module 16's report stated this view was already built: `explain_case` over
a Module 15 CASE record. So this file does three things and none of them
is interpretation — find the setup's outcome, ask Module 15 to assemble
its record, ask Module 16 to narrate it, and put the result on the wire.

The temptation this resists is writing a sentence. A failure is the most
interesting thing ARGUS produces and the place where prose would be most
welcome, which is exactly why Module 16 built a fabrication guard around
that prose — every sentence cites a fact from the input, and a verifier
rejects one that does not. Writing "the base broke down on heavy volume"
here would produce a claim nothing checks.

## Failures and successes take the identical path

Module 15 built its case records so a failure is as complete as a success,
and Module 16's `explain_case` has no branch on classification. This file
adds none either: `read_case` is called for any concluded setup and the
word "fail" appears in the endpoint's name rather than in its logic.

## The caveat travels with the label

`false_positive_type` is Module 15's A–D classification, and Module 15
said plainly that its thresholds are unvalidated — the A–D boundary sits
at a 3% MFE floor nobody has checked against real outcomes. So the
response carries `classification_caveat` beside the label. A label without
its stated limits reads as a finding; with them it reads as what it is.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.engine import Connection

from core.explanation.narrators import explain_case
from core.outcome_tracking.engine import compute_case
from infra.db.schema.setups import setup_outcomes, setups
from services.intelligence.blocks import build_explanation, build_freshness
from services.intelligence.errors import (
    NOT_CONCLUDED,
    SETUP_NOT_FOUND,
    IntelligenceError,
)
from services.intelligence.schemas import CaseExplanation

__all__ = ["CLASSIFICATION_CAVEAT", "read_case"]

CLASSIFICATION_CAVEAT = (
    "Module 15 classifies a false positive into one of four types using thresholds it "
    "states are unvalidated — the boundary between types sits at a maximum-favourable-"
    "excursion floor nobody has yet checked against real outcomes. Treat the type as a "
    "starting point for reading the case, not as a measured finding. The excursion "
    "numbers and the outcome status beside it are measurements; the type is a "
    "judgement about them."
)


def read_case(
    connection: Connection,
    setup_id: UUID,
    *,
    as_of: datetime,
    stale_after_seconds: float,
) -> CaseExplanation:
    """What happened to a concluded setup, and why.

    Refuses an unconcluded setup with `NOT_CONCLUDED` rather than a 404:
    the setup exists and is perfectly healthy, it simply has no outcome
    yet, and telling a caller it does not exist would be wrong in a way
    they could not diagnose.
    """
    row = connection.execute(
        select(
            setups.c.id,
            setups.c.security_id,
            setups.c.concluded_at,
            setup_outcomes.c.outcome_status,
            setup_outcomes.c.false_positive_type,
            setup_outcomes.c.review_confidence,
            setup_outcomes.c.data_snapshot_id,
            setup_outcomes.c.recorded_at,
        )
        .select_from(setups.outerjoin(setup_outcomes, setup_outcomes.c.setup_id == setups.c.id))
        .where(setups.c.id == setup_id)
        # Since migration 0007 a setup may carry one outcome per snapshot.
        # The most recently recorded is the labelling ARGUS currently
        # believes, which is the one to explain.
        .order_by(desc(setup_outcomes.c.recorded_at))
        .limit(1)
    ).one_or_none()

    if row is None:
        raise IntelligenceError(
            SETUP_NOT_FOUND,
            f"No setup {setup_id}.",
            status=404,
            detail={"setup_id": str(setup_id)},
        )

    if row.outcome_status is None:
        raise IntelligenceError(
            NOT_CONCLUDED,
            "This setup has not concluded, so there is no outcome to explain. ARGUS is "
            "still tracking it.",
            status=409,
            detail={"setup_id": str(setup_id)},
        )

    # Module 15 assembles its own record; Module 16 narrates it. Neither
    # is reimplemented here, and the `as_of` bound travels into both so a
    # historical view stays historical.
    case = compute_case(
        connection,
        setup_id,
        as_of=as_of,
        data_snapshot_id=row.data_snapshot_id,
    )
    payload = case.as_dict()
    narration = explain_case(payload)

    # Module 15 nests its classification under `verdict` — the status,
    # the reason, the false-positive type and how much each is trusted.
    classification = payload.get("verdict") or {}
    return CaseExplanation(
        setup_id=row.id,
        security_id=row.security_id,
        outcome_status=str(row.outcome_status),
        explanation=build_explanation(narration),
        false_positive_type=(str(row.false_positive_type) if row.false_positive_type else None),
        false_positive_confidence=classification.get("false_positive_confidence"),
        review_confidence=(str(row.review_confidence) if row.review_confidence else None),
        classification_caveat=CLASSIFICATION_CAVEAT,
        freshness=build_freshness(
            computed_at=row.recorded_at, as_of=as_of, stale_after_seconds=stale_after_seconds
        ),
    )
