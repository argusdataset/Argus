"""PIT-safe universe membership queries, and interval_evidence surfacing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import Connection

from core.data_validation.entities import EntityType
from core.data_validation.query import get_as_of
from core.data_validation.result import MissReason
from core.data_validation.universe import (
    get_universe_membership_as_of,
    list_universe_members_as_of,
    parse_interval_evidence,
)
from core.universe.intervals import IntervalEvidence
from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from infra.db.enums import ListingStatus
from infra.db.schema.identity import universe_membership, universe_version


@pytest.fixture
def universe_version_id(connection: Connection) -> UUID:
    return connection.execute(
        universe_version.insert()
        .values(
            version_label=f"test-{uuid4().hex[:8]}",
            as_of_date=datetime(2026, 1, 1, tzinfo=UTC),
            definition={},
        )
        .returning(universe_version.c.id)
    ).scalar_one()


def _add_member(
    connection: Connection,
    universe_version_id: UUID,
    security_id: UUID,
    *,
    listed_from: datetime,
    listed_to: datetime | None,
    listing_status: ListingStatus = ListingStatus.LISTED,
    evidence: str = "from=delisted_feed;to=delisted_feed",
    exchange: CanonicalExchange = CanonicalExchange.NYSE,
) -> None:
    connection.execute(
        universe_membership.insert().values(
            universe_version_id=universe_version_id,
            security_id=security_id,
            listing_status=listing_status.value,
            listed_from=listed_from,
            listed_to=listed_to,
            exchange=exchange.value,
            interval_evidence=evidence,
        )
    )


def _security(connection: Connection, symbol: str) -> UUID:
    resolver = SecurityIdentityResolver(connection)
    return resolver.register(
        symbol, exchange=CanonicalExchange.NYSE, valid_from=datetime(1990, 1, 1, tzinfo=UTC)
    )


# --------------------------------------------------------------------------
# Interval predicate
# --------------------------------------------------------------------------


def test_a_date_within_the_interval_is_a_member(connection: Connection, universe_version_id: UUID):
    security_id = _security(connection, "AAAA")
    _add_member(
        connection,
        universe_version_id,
        security_id,
        listed_from=datetime(2000, 1, 1, tzinfo=UTC),
        listed_to=datetime(2010, 1, 1, tzinfo=UTC),
    )

    result = get_universe_membership_as_of(
        connection, universe_version_id, security_id, datetime(2005, 1, 1, tzinfo=UTC)
    )
    assert result.found


def test_a_date_before_listing_is_not_a_member(connection: Connection, universe_version_id: UUID):
    security_id = _security(connection, "BBBB")
    _add_member(
        connection,
        universe_version_id,
        security_id,
        listed_from=datetime(2000, 1, 1, tzinfo=UTC),
        listed_to=None,
    )

    result = get_universe_membership_as_of(
        connection, universe_version_id, security_id, datetime(1995, 1, 1, tzinfo=UTC)
    )
    assert not result
    assert result.reason is MissReason.OUTSIDE_INTERVAL


def test_the_delisting_date_itself_is_excluded_half_open(
    connection: Connection, universe_version_id: UUID
):
    security_id = _security(connection, "CCCC")
    delisted_at = datetime(2008, 9, 17, tzinfo=UTC)
    _add_member(
        connection,
        universe_version_id,
        security_id,
        listed_from=datetime(1994, 1, 1, tzinfo=UTC),
        listed_to=delisted_at,
    )

    still_in = get_universe_membership_as_of(
        connection, universe_version_id, security_id, delisted_at - timedelta(days=1)
    )
    exactly_out = get_universe_membership_as_of(
        connection, universe_version_id, security_id, delisted_at
    )
    assert still_in.found
    assert not exactly_out.found


def test_null_listed_to_means_still_listed(connection: Connection, universe_version_id: UUID):
    security_id = _security(connection, "DDDD")
    _add_member(
        connection,
        universe_version_id,
        security_id,
        listed_from=datetime(2000, 1, 1, tzinfo=UTC),
        listed_to=None,
    )

    far_future = get_universe_membership_as_of(
        connection, universe_version_id, security_id, datetime(2099, 1, 1, tzinfo=UTC)
    )
    assert far_future.found


def test_a_security_absent_from_the_version_is_a_miss(
    connection: Connection, universe_version_id: UUID
):
    never_added = uuid4()
    result = get_universe_membership_as_of(
        connection, universe_version_id, never_added, datetime(2020, 1, 1, tzinfo=UTC)
    )
    assert not result
    assert result.reason is MissReason.OUTSIDE_INTERVAL


# --------------------------------------------------------------------------
# interval_evidence surfacing — Module 06's explicit instruction
# --------------------------------------------------------------------------


def test_interval_evidence_is_parsed_and_present_not_dropped(
    connection: Connection, universe_version_id: UUID
):
    """Module 06: a first_observed boundary is a materially weaker claim
    than a delisted_feed one, and a query result must not erase that.
    """
    security_id = _security(connection, "EEEE")
    _add_member(
        connection,
        universe_version_id,
        security_id,
        listed_from=datetime(2020, 1, 1, tzinfo=UTC),
        listed_to=None,
        evidence="from=first_observed;to=missing",
    )

    result = get_universe_membership_as_of(
        connection, universe_version_id, security_id, datetime(2021, 1, 1, tzinfo=UTC)
    )
    membership = result.unwrap()
    assert membership.from_evidence is IntervalEvidence.FIRST_OBSERVED
    assert membership.to_evidence is IntervalEvidence.MISSING


def test_delisted_feed_evidence_is_distinguishable_from_first_observed(
    connection: Connection, universe_version_id: UUID
):
    strong = _security(connection, "FFFF")
    weak = _security(connection, "GGGG")
    _add_member(
        connection,
        universe_version_id,
        strong,
        listed_from=datetime(2000, 1, 1, tzinfo=UTC),
        listed_to=datetime(2010, 1, 1, tzinfo=UTC),
        evidence="from=delisted_feed;to=delisted_feed",
    )
    _add_member(
        connection,
        universe_version_id,
        weak,
        listed_from=datetime(2005, 1, 1, tzinfo=UTC),
        listed_to=None,
        evidence="from=first_observed;to=missing",
    )

    members = list_universe_members_as_of(
        connection, universe_version_id, datetime(2006, 1, 1, tzinfo=UTC)
    )
    evidence_by_security = {m.security_id: m.from_evidence for m in members}
    assert evidence_by_security[strong] is IntervalEvidence.DELISTED_FEED
    assert evidence_by_security[weak] is IntervalEvidence.FIRST_OBSERVED


def test_parse_interval_evidence_handles_the_stored_label_format():
    from_ev, to_ev = parse_interval_evidence("from=price_history;to=missing")
    assert from_ev is IntervalEvidence.PRICE_HISTORY
    assert to_ev is IntervalEvidence.MISSING


def test_parse_interval_evidence_degrades_gracefully_on_an_unrecognised_label():
    """A query layer must not raise on a shape it doesn't recognise."""
    from_ev, to_ev = parse_interval_evidence("garbage")
    assert from_ev is IntervalEvidence.MISSING
    assert to_ev is IntervalEvidence.MISSING


# --------------------------------------------------------------------------
# list_universe_members_as_of
# --------------------------------------------------------------------------


def test_list_members_excludes_securities_outside_their_interval(
    connection: Connection, universe_version_id: UUID
):
    in_range = _security(connection, "HHHH")
    out_of_range = _security(connection, "IIII")
    _add_member(
        connection,
        universe_version_id,
        in_range,
        listed_from=datetime(2000, 1, 1, tzinfo=UTC),
        listed_to=None,
    )
    _add_member(
        connection,
        universe_version_id,
        out_of_range,
        listed_from=datetime(2020, 1, 1, tzinfo=UTC),
        listed_to=None,
    )

    members = list_universe_members_as_of(
        connection, universe_version_id, datetime(2010, 1, 1, tzinfo=UTC)
    )
    ids = {m.security_id for m in members}
    assert in_range in ids
    assert out_of_range not in ids


# --------------------------------------------------------------------------
# get_as_of dispatcher
# --------------------------------------------------------------------------


def test_get_as_of_dispatcher_handles_universe_membership(
    connection: Connection, universe_version_id: UUID
):
    security_id = _security(connection, "JJJJ")
    _add_member(
        connection,
        universe_version_id,
        security_id,
        listed_from=datetime(2000, 1, 1, tzinfo=UTC),
        listed_to=None,
    )

    result = get_as_of(
        connection,
        EntityType.UNIVERSE_MEMBERSHIP,
        security_id,
        datetime(2010, 1, 1, tzinfo=UTC),
        universe_version_id=universe_version_id,
    )
    assert result.found
    assert result.unwrap().from_evidence is IntervalEvidence.DELISTED_FEED


def test_get_as_of_requires_universe_version_id_for_membership_queries(connection: Connection):
    with pytest.raises(ValueError, match="universe_version_id"):
        get_as_of(
            connection,
            EntityType.UNIVERSE_MEMBERSHIP,
            uuid4(),
            datetime(2010, 1, 1, tzinfo=UTC),
        )
