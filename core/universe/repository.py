"""Reading and writing universe versions.

`universe_version` and `universe_membership` both carry Module 03's
append-only triggers, so everything here is insert-only. That is the
right constraint: a signal records the `universe_version_id` it was
computed against, and if a published version's membership could be edited
afterwards, every historical result referencing it would become
unverifiable.

Re-running construction for an unchanged universe therefore must not
create a second version. Each version stores a content checksum over its
membership, and `find_equivalent` looks for an existing version with the
same as-of date and checksum before a new one is written.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.universe.intervals import ListingInterval
from infra.db.schema.identity import universe_membership, universe_version

#: Rows per INSERT when writing membership.
MEMBERSHIP_BATCH_SIZE = 1_000


@dataclass(frozen=True, slots=True)
class StoredUniverseVersion:
    """A persisted universe version."""

    id: UUID
    version_label: str
    as_of_date: datetime
    member_count: int
    #: True when construction reused an existing, identical version.
    reused: bool = False


def membership_checksum(intervals: list[ListingInterval]) -> str:
    """Stable checksum over a version's membership.

    Covers identity, venue and both interval boundaries — everything that
    defines the membership — so a reclassification or a corrected listing
    date produces a different checksum and therefore a new version, while
    a re-run over unchanged data does not.

    Sorted before hashing so construction order cannot affect the result.
    """
    payload = sorted(
        (
            str(interval.security_id),
            interval.exchange.value,
            interval.listed_from.isoformat(),
            interval.listed_to.isoformat() if interval.listed_to else "",
            interval.listing_status.value,
        )
        for interval in intervals
    )
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class UniverseRepository:
    """Insert-only access to universe versions and their membership."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def find_equivalent(self, as_of: datetime, checksum: str) -> StoredUniverseVersion | None:
        """An existing version for this as-of date with identical membership.

        Matching on the checksum inside `definition` rather than on the
        label keeps the check about *content*: two runs that produce the
        same universe should not create two versions, however they were
        labelled.
        """
        row = self._connection.execute(
            select(
                universe_version.c.id,
                universe_version.c.version_label,
                universe_version.c.as_of_date,
                universe_version.c.definition,
            )
            .where(
                universe_version.c.as_of_date == as_of,
                universe_version.c.definition["membership_checksum"].astext == checksum,
            )
            .limit(1)
        ).one_or_none()

        if row is None:
            return None
        return StoredUniverseVersion(
            id=row.id,
            version_label=row.version_label,
            as_of_date=row.as_of_date,
            member_count=int(row.definition.get("member_count", 0)),
            reused=True,
        )

    def create_version(
        self,
        *,
        version_label: str,
        as_of: datetime,
        definition: dict[str, Any],
        description: str | None = None,
    ) -> UUID:
        """Insert a new immutable version record."""
        return self._connection.execute(
            universe_version.insert()
            .values(
                version_label=version_label,
                as_of_date=as_of,
                definition=definition,
                description=description,
            )
            .returning(universe_version.c.id)
        ).scalar_one()

    def add_members(self, version_id: UUID, intervals: list[ListingInterval]) -> int:
        """Attach membership rows to a version, insert-only.

        ON CONFLICT DO NOTHING makes an interrupted construction safe to
        retry: rows already written are skipped. DO NOTHING rather than DO
        UPDATE because the latter would fire the append-only trigger — the
        uniqueness constraint and the guard agree that membership, once
        written, is final.
        """
        rows = [
            {
                "universe_version_id": version_id,
                "security_id": interval.security_id,
                "listing_status": interval.listing_status.value,
                "listed_from": interval.listed_from,
                "listed_to": interval.listed_to,
                "exchange": interval.exchange.value,
                "interval_evidence": interval.evidence_label,
            }
            for interval in intervals
        ]

        inserted = 0
        for start in range(0, len(rows), MEMBERSHIP_BATCH_SIZE):
            batch = rows[start : start + MEMBERSHIP_BATCH_SIZE]
            if not batch:
                continue
            statement = (
                insert(universe_membership)
                .values(batch)
                .on_conflict_do_nothing(index_elements=["universe_version_id", "security_id"])
                .returning(universe_membership.c.id)
            )
            inserted += len(self._connection.execute(statement).fetchall())
        return inserted

    def members(self, version_id: UUID) -> list[dict[str, Any]]:
        """Every membership row for a version."""
        rows = self._connection.execute(
            select(
                universe_membership.c.security_id,
                universe_membership.c.listing_status,
                universe_membership.c.listed_from,
                universe_membership.c.listed_to,
                universe_membership.c.exchange,
                universe_membership.c.interval_evidence,
            ).where(universe_membership.c.universe_version_id == version_id)
        ).all()
        return [dict(row._mapping) for row in rows]

    def member_count(self, version_id: UUID) -> int:
        return len(self.members(version_id))


def default_version_label(as_of: datetime, checksum: str) -> str:
    """A readable, deterministic label.

    The as-of date makes versions sortable and scannable by eye; the
    checksum prefix keeps two different universes for the same date
    distinguishable without a counter.
    """
    return f"universe-{as_of.astimezone(UTC).date().isoformat()}-{checksum[:12]}"
