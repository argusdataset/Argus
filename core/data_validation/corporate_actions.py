"""PIT-safe access to canonical corporate actions.

The use this exists for: computing a PIT-correct adjusted price series
means applying only the splits and dividends that were *knowable* as of
the date being computed for, not every action ARGUS has since ingested.
Module 05's `apply_adjustments` applies everything it is given — that is
correct for building the canonical adjusted series (which is meant to
reflect final, complete knowledge), but a feature computed "as of" a past
date needs the narrower, as-knowable-then list this module provides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Connection

from core.data_validation.engine import select_all_as_of
from data.canonical_model.records import CanonicalCorporateActionType
from infra.db.schema.canonical import canonical_corporate_actions


@dataclass(frozen=True, slots=True)
class CorporateActionAsOf:
    """One corporate action, as it was known at query time."""

    security_id: UUID
    action_type: CanonicalCorporateActionType
    effective_date: date
    observation_time: datetime
    availability_time: datetime
    details: dict[str, Any]


def get_corporate_actions_as_of(
    connection: Connection,
    security_id: UUID,
    as_of: datetime,
) -> list[CorporateActionAsOf]:
    """Every corporate action for `security_id` knowable by `as_of`.

    Not a single-record lookup like OHLCV or fundamentals — a security
    can have many actions, and the caller (a PIT-correct adjustment
    computation) needs the whole knowable set, ordered by when they took
    effect. An empty list is a legitimate answer (no actions were knowable
    yet), not distinguished from "none exist at all"; the caller here
    doesn't need that distinction the way a single-value lookup does.
    """
    rows = select_all_as_of(
        connection,
        canonical_corporate_actions,
        key={"security_id": security_id},
        as_of=as_of,
        order_by="effective_date",
    )
    return [
        CorporateActionAsOf(
            security_id=row.security_id,
            action_type=CanonicalCorporateActionType(row.action_type),
            effective_date=row.effective_date.date()
            if isinstance(row.effective_date, datetime)
            else row.effective_date,
            observation_time=row.observation_time,
            availability_time=row.availability_time,
            details=dict(row.details),
        )
        for row in rows
    ]
