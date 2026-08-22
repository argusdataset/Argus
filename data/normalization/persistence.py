"""Writing canonical records to Module 03's tables.

Every write is an INSERT. There is no update path and no delete path in
this module, by construction rather than by discipline — the canonical
tables carry append-only triggers (migration 0003), so an UPDATE issued
from anywhere, including here, is rejected by the database.

That matters because of how restatements work. A revised fundamental or a
corrected bar arrives as a *new row* with a later `observation_time`,
never as an edit to the existing one. Module 03's uniqueness constraints
include `observation_time` for exactly this reason: the same
`event_time` legitimately has several observations, and a
point-in-time-correct query picks the latest one that was available at
the time being asked about. Overwriting the earlier row would destroy the
record of what ARGUS believed back then, which is the thing that makes a
historical claim checkable.

Re-ingestion is idempotent: inserts use ON CONFLICT DO NOTHING against
those same constraints, so re-running an interrupted backfill inserts
what is missing and skips what is already there. DO NOTHING is used
rather than DO UPDATE precisely because the latter would fire the
append-only trigger and fail — the constraint and the guard agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from data.canonical_model.records import (
    CanonicalCorporateAction,
    CanonicalFundamental,
    CanonicalOhlcvBar,
)
from infra.db.schema.canonical import (
    canonical_corporate_actions,
    canonical_fundamentals,
    canonical_ohlcv,
)

#: Rows per INSERT. Large enough that a full-universe backfill is not
#: dominated by round trips, small enough to keep statements readable in
#: a slow-query log and memory bounded.
DEFAULT_BATCH_SIZE = 1_000


@dataclass(slots=True)
class WriteResult:
    """How many rows were offered, and how many were actually new."""

    offered: int = 0
    inserted: int = 0

    @property
    def skipped(self) -> int:
        """Rows already present — the normal case when resuming a backfill."""
        return self.offered - self.inserted


class CanonicalWriter:
    """Persists canonical records. Insert-only.

    Takes a `Connection` rather than an `Engine` so the caller controls
    the transaction boundary: a backfill batching thousands of bars per
    security wants one transaction per security, not one per row.
    """

    def __init__(self, connection: Connection, *, batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        self._connection = connection
        self._batch_size = batch_size

    def write_bars(self, bars: list[CanonicalOhlcvBar]) -> WriteResult:
        return self._insert(
            canonical_ohlcv,
            [self._bar_values(bar) for bar in bars],
            conflict_columns=("security_id", "timeframe", "event_time", "observation_time"),
        )

    def write_fundamentals(self, statements: list[CanonicalFundamental]) -> WriteResult:
        return self._insert(
            canonical_fundamentals,
            [self._fundamental_values(statement) for statement in statements],
            conflict_columns=("security_id", "statement_type", "fiscal_period", "observation_time"),
        )

    def write_corporate_actions(self, actions: list[CanonicalCorporateAction]) -> WriteResult:
        return self._insert(
            canonical_corporate_actions,
            [self._action_values(action) for action in actions],
            conflict_columns=("security_id", "action_type", "effective_date", "observation_time"),
        )

    def _insert(
        self,
        table: Any,
        rows: list[dict[str, Any]],
        *,
        conflict_columns: tuple[str, ...],
    ) -> WriteResult:
        result = WriteResult(offered=len(rows))
        for start in range(0, len(rows), self._batch_size):
            batch = rows[start : start + self._batch_size]
            if not batch:
                continue
            statement = (
                insert(table)
                .values(batch)
                # DO NOTHING, never DO UPDATE: an existing row is a fact
                # ARGUS already recorded, and rewriting it would erase
                # what was believed at that time.
                .on_conflict_do_nothing(index_elements=list(conflict_columns))
                .returning(table.c.id)
            )
            result.inserted += len(self._connection.execute(statement).fetchall())
        return result

    # -- Row construction ---------------------------------------------------

    @staticmethod
    def _bar_values(bar: CanonicalOhlcvBar) -> dict[str, Any]:
        return {
            "security_id": bar.security_id,
            "timeframe": bar.timeframe.value,
            **bar.pit.as_columns(),
            "open_raw": bar.open_raw,
            "high_raw": bar.high_raw,
            "low_raw": bar.low_raw,
            "close_raw": bar.close_raw,
            "volume_raw": bar.volume_raw,
            "open_adjusted": bar.open_adjusted,
            "high_adjusted": bar.high_adjusted,
            "low_adjusted": bar.low_adjusted,
            "close_adjusted": bar.close_adjusted,
            "volume_adjusted": bar.volume_adjusted,
        }

    @staticmethod
    def _fundamental_values(statement: CanonicalFundamental) -> dict[str, Any]:
        return {
            "security_id": statement.security_id,
            "statement_type": statement.statement_type.value,
            "fiscal_period": statement.fiscal_period,
            "fiscal_period_end": statement.fiscal_period_end,
            **statement.pit.as_columns(),
            "data": statement.data,
        }

    @staticmethod
    def _action_values(action: CanonicalCorporateAction) -> dict[str, Any]:
        return {
            "security_id": action.security_id,
            "action_type": action.action_type.value,
            **action.pit.as_columns(),
            "effective_date": action.effective_date,
            "details": action.details,
        }
