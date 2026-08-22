"""PIT-safe access to universe membership.

Universe membership isn't filtered on `availability_time` the way the
other entities are — Module 06 designed a different, and for this entity
correct, mechanism: a half-open listing interval per security
(`listed_from <= as_of AND (listed_to IS NULL OR listed_to > as_of)`).
This module applies that predicate rather than inventing a parallel one.

**Scope boundary worth stating precisely.** A `universe_version` is
already a snapshot as of its own `as_of_date` — Module 06's
`construct_version` only persists the members covering that date. This
module does not build new snapshots for arbitrary dates; that is Module
06's `construct_version`, already built. What this module adds on top of
an *existing* version's membership rows:

1. **Re-applies the interval predicate defensively** against whatever
   `as_of` the caller actually passes, using the row's own
   `listed_from`/`listed_to`, rather than trusting that a row present in
   the table must mean "in the universe" without checking. If a caller
   passes an `as_of` that does not match the version's construction date,
   the predicate still gives the correct answer for that row's own
   interval — or correctly reports it out of range.
2. **Surfaces `interval_evidence`**, parsed back into the same
   `IntervalEvidence` values Module 06 assigned, rather than the single
   opaque string stored in the column. A boundary inferred from
   `first_observed` is a materially weaker claim than one from
   `delisted_feed`; treating them identically overstates confidence in
   historical membership.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Connection

from core.data_validation.result import AsOfResult, MissReason
from core.universe.intervals import IntervalEvidence
from data.canonical_model.exchanges import CanonicalExchange
from infra.db.enums import ListingStatus
from infra.db.schema.identity import universe_membership

#: Fallback when a row predates this module and has no parseable evidence
#: label — should not occur for anything written by Module 06's current
#: repository, but a query layer must not raise on a shape it does not
#: recognise.
_UNPARSEABLE = IntervalEvidence.MISSING


@dataclass(frozen=True, slots=True)
class MembershipAsOf:
    """One security's universe membership, as it was known at query time."""

    security_id: UUID
    universe_version_id: UUID
    listing_status: ListingStatus
    exchange: CanonicalExchange
    listed_from: datetime
    listed_to: datetime | None
    from_evidence: IntervalEvidence
    to_evidence: IntervalEvidence


def parse_interval_evidence(label: str) -> tuple[IntervalEvidence, IntervalEvidence]:
    """Recover the (from, to) evidence pair Module 06 encoded as one string."""
    parts = dict(item.split("=", 1) for item in label.split(";") if "=" in item)
    try:
        return IntervalEvidence(parts["from"]), IntervalEvidence(parts["to"])
    except (KeyError, ValueError):
        return _UNPARSEABLE, _UNPARSEABLE


def _row_to_membership(row) -> MembershipAsOf:
    from_evidence, to_evidence = parse_interval_evidence(row.interval_evidence)
    return MembershipAsOf(
        security_id=row.security_id,
        universe_version_id=row.universe_version_id,
        listing_status=ListingStatus(row.listing_status),
        exchange=CanonicalExchange(row.exchange),
        listed_from=row.listed_from,
        listed_to=row.listed_to,
        from_evidence=from_evidence,
        to_evidence=to_evidence,
    )


def _interval_predicate(as_of: datetime):
    return and_(
        universe_membership.c.listed_from <= as_of,
        or_(universe_membership.c.listed_to.is_(None), universe_membership.c.listed_to > as_of),
    )


def get_universe_membership_as_of(
    connection: Connection,
    universe_version_id: UUID,
    security_id: UUID,
    as_of: datetime,
) -> AsOfResult[MembershipAsOf]:
    """Whether `security_id` was a universe member at `as_of`, with evidence.

    A miss means the security's listing interval in this version does not
    cover `as_of` — it was not yet listed, or had already delisted. It
    does *not* mean the security is unknown to ARGUS; that distinction is
    Module 06's identity resolution, not this query.
    """
    query = select(universe_membership).where(
        universe_membership.c.universe_version_id == universe_version_id,
        universe_membership.c.security_id == security_id,
        _interval_predicate(as_of),
    )
    row = connection.execute(query).first()
    if row is None:
        return AsOfResult.miss(MissReason.OUTSIDE_INTERVAL, as_of=as_of)
    return AsOfResult.hit(_row_to_membership(row), as_of=as_of)


def list_universe_members_as_of(
    connection: Connection,
    universe_version_id: UUID,
    as_of: datetime,
) -> list[MembershipAsOf]:
    """Every security whose listing interval in this version covers `as_of`.

    Each result carries `interval_evidence`, so a caller — historical
    similarity comparing across eras, or model evaluation weighting
    sample quality — can filter or down-weight `first_observed`-sourced
    boundaries rather than treating every membership claim as equally
    solid.
    """
    query = (
        select(universe_membership)
        .where(
            universe_membership.c.universe_version_id == universe_version_id,
            _interval_predicate(as_of),
        )
        .order_by(universe_membership.c.security_id)
    )
    return [_row_to_membership(row) for row in connection.execute(query)]
