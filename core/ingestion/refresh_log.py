"""Reading and writing the deep-refresh log.

Two operations: "when was each of these securities last refreshed, and
under which phase", and "record that this one just was".

## The read is one query, not one per security

`DISTINCT ON (security_id) ... ORDER BY security_id, refreshed_on DESC`
gives the latest row per security in a single pass. At ten thousand
names that is the difference between one round trip and ten thousand,
and this read happens on every run.

`DISTINCT ON` is Postgres-specific. So is every other query in this
project — the append-only guards are triggers, the enums are native
types, and `infra/db/migrations` targets Postgres exclusively — so there
is no portability being given up here.

## The write is insert-only, and the constraint is the idempotency

`(security_id, refreshed_on)` is unique, so a second attempt to record
the same security on the same run date conflicts and does nothing. That
is what makes a re-run of the same day a no-op in the database rather
than only in a checkpoint file, which matters because a Railway cron
container starts with an empty filesystem and the checkpoint does not
survive between firings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.ingestion.tiers import RefreshRecord
from infra.db.enums import MarketState
from infra.db.schema.ingestion import deep_refresh_log

__all__ = ["CompletedRefresh", "last_refreshes", "record_refresh"]

_LATEST_QUERY = """
    SELECT DISTINCT ON (security_id)
           security_id, refreshed_on, triggering_watchlist
    FROM deep_refresh_log
    WHERE security_id = ANY(:security_ids)
    ORDER BY security_id, refreshed_on DESC
"""


@dataclass(frozen=True, slots=True)
class CompletedRefresh:
    """What one finished refresh wrote, for the log row and the report."""

    security_id: UUID
    refreshed_on: date
    triggering_watchlist: str
    market_state: MarketState
    trigger: str
    statements_written: int
    news_written: int
    config_version_label: str
    detail: dict[str, Any]


def last_refreshes(
    connection: Connection,
    security_ids: list[UUID],
) -> dict[UUID, RefreshRecord]:
    """The most recent completed refresh per security, where there is one.

    Returns `tiers.RefreshRecord` — the two fields the due-ness decision
    actually needs — rather than the whole row, so nothing downstream can
    accidentally read `triggering_watchlist` as a current phase.
    """
    if not security_ids:
        return {}

    rows = connection.execute(text(_LATEST_QUERY), {"security_ids": security_ids}).all()
    return {
        row.security_id: RefreshRecord(
            watchlist=row.triggering_watchlist,
            refreshed_on=row.refreshed_on,
        )
        for row in rows
    }


def record_refresh(connection: Connection, completed: CompletedRefresh) -> bool:
    """Log one completed refresh. False if this security/date is already logged."""
    statement = (
        insert(deep_refresh_log)
        .values(
            security_id=completed.security_id,
            refreshed_on=completed.refreshed_on,
            triggering_watchlist=completed.triggering_watchlist,
            market_state=completed.market_state.value,
            trigger=completed.trigger,
            statements_written=completed.statements_written,
            news_written=completed.news_written,
            config_version_label=completed.config_version_label,
            detail=completed.detail,
        )
        .on_conflict_do_nothing(constraint="uq_deep_refresh_security_date")
        .returning(deep_refresh_log.c.id)
    )
    return connection.execute(statement).scalar_one_or_none() is not None
