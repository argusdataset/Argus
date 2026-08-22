"""The generic selection primitive, including its `precedence` ordering.

`select_latest_as_of` is the single place the PIT enforcement rule is
written, so its own behaviour deserves tests that do not go through an
entity-specific wrapper. Added when `fundamentals.py`'s two hand-written
copies of the rule were consolidated onto it — that consolidation
introduced `precedence`, and a new parameter on the chokepoint is exactly
the code that must not be tested only indirectly.

The load-bearing test here is
`test_precedence_cannot_weaken_the_availability_filter`. `precedence`
exists to change *ordering*; if it could ever change *which rows are
visible*, it would be a way to bypass the one rule this whole package
exists to enforce.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection

from core.data_validation.engine import select_latest_as_of
from data.canonical_model.records import CanonicalStatementType
from infra.db.schema.canonical import canonical_fundamentals

Q1_END = datetime(2019, 3, 31, tzinfo=UTC)
Q2_END = datetime(2019, 6, 30, tzinfo=UTC)

Q1_ORIGINAL_AVAILABLE = datetime(2019, 5, 15, tzinfo=UTC)
Q2_AVAILABLE = datetime(2019, 8, 15, tzinfo=UTC)
#: The Q1 correction lands *after* Q2 was filed — the case that separates
#: "latest filed" from "latest period".
Q1_RESTATED_AVAILABLE = datetime(2019, 9, 20, tzinfo=UTC)

AFTER_EVERYTHING = datetime(2019, 10, 1, tzinfo=UTC)


def _insert(
    connection: Connection,
    security_id: UUID,
    *,
    period_end: datetime,
    available: datetime,
    revenue: int,
) -> None:
    connection.execute(
        canonical_fundamentals.insert().values(
            security_id=security_id,
            statement_type=CanonicalStatementType.INCOME_STATEMENT.value,
            fiscal_period="Q1" if period_end == Q1_END else "Q2",
            fiscal_period_end=period_end,
            event_time=period_end,
            observation_time=available,
            availability_time=available,
            ingestion_time=available,
            data={"revenue": revenue},
        )
    )


@pytest.fixture
def out_of_order_filings(connection: Connection, security_id: UUID) -> UUID:
    """Q1 filed, Q2 filed, then Q1 restated — in that order in time.

    The ordering that matters: the *most recently filed* row belongs to
    the *earlier* period, so "greatest availability_time" and "latest
    fiscal period" pick different rows.
    """
    _insert(
        connection, security_id, period_end=Q1_END, available=Q1_ORIGINAL_AVAILABLE, revenue=100
    )
    _insert(connection, security_id, period_end=Q2_END, available=Q2_AVAILABLE, revenue=200)
    _insert(
        connection, security_id, period_end=Q1_END, available=Q1_RESTATED_AVAILABLE, revenue=111
    )
    return security_id


def _key(security_id: UUID) -> dict[str, object]:
    return {
        "security_id": security_id,
        "statement_type": CanonicalStatementType.INCOME_STATEMENT.value,
    }


def test_without_precedence_the_greatest_availability_time_wins(
    connection: Connection, out_of_order_filings: UUID
):
    """The default: the most recently knowable row, whatever period it is.

    Correct when `key` pins one logical record — every row is then a
    restatement of the same fact. Asserted explicitly so the contrast with
    the next test is a documented difference rather than an accident.
    """
    row = select_latest_as_of(
        connection,
        canonical_fundamentals,
        key=_key(out_of_order_filings),
        as_of=AFTER_EVERYTHING,
    )
    assert row is not None
    assert row.data["revenue"] == 111  # the Q1 restatement, filed last
    assert row.fiscal_period_end == Q1_END


def test_precedence_picks_the_later_period_over_the_later_filing(
    connection: Connection, out_of_order_filings: UUID
):
    """`precedence` orders by its columns DESC *before* availability_time.

    This is what `get_latest_fundamental_as_of` needs: the freshest
    *period* ARGUS could have used, with that period's freshest
    restatement — not whichever row happened to be filed most recently.
    """
    row = select_latest_as_of(
        connection,
        canonical_fundamentals,
        key=_key(out_of_order_filings),
        as_of=AFTER_EVERYTHING,
        precedence=("fiscal_period_end",),
    )
    assert row is not None
    assert row.data["revenue"] == 200  # Q2
    assert row.fiscal_period_end == Q2_END


def test_precedence_still_takes_the_latest_restatement_within_a_period(
    connection: Connection, security_id: UUID
):
    """Both halves at once: latest period, and within it latest known row.

    A `precedence` that ordered only on the period would return whichever
    restatement the database happened to hand back first.
    """
    _insert(connection, security_id, period_end=Q2_END, available=Q2_AVAILABLE, revenue=200)
    _insert(
        connection,
        security_id,
        period_end=Q2_END,
        available=datetime(2019, 9, 25, tzinfo=UTC),
        revenue=222,
    )

    row = select_latest_as_of(
        connection,
        canonical_fundamentals,
        key=_key(security_id),
        as_of=AFTER_EVERYTHING,
        precedence=("fiscal_period_end",),
    )
    assert row is not None
    assert row.data["revenue"] == 222


def test_precedence_cannot_weaken_the_availability_filter(
    connection: Connection, out_of_order_filings: UUID
):
    """The invariant that makes the new parameter safe to have.

    `precedence` changes ordering only. A row that is not yet knowable
    must stay invisible no matter what ordering is requested — otherwise
    the parameter would be a way to reach around the one rule this
    package exists to enforce.

    Queried on 1 September: Q2 is knowable, the Q1 restatement (filed the
    20th) is not.
    """
    before_the_q1_restatement = datetime(2019, 9, 1, tzinfo=UTC)

    row = select_latest_as_of(
        connection,
        canonical_fundamentals,
        key=_key(out_of_order_filings),
        as_of=before_the_q1_restatement,
        precedence=("fiscal_period_end",),
    )
    assert row is not None
    assert row.data["revenue"] == 200
    assert row.availability_time <= before_the_q1_restatement

    # And with no precedence at all — the filter is upstream of ordering,
    # so neither spelling can see the September filing.
    unordered = select_latest_as_of(
        connection,
        canonical_fundamentals,
        key=_key(out_of_order_filings),
        as_of=before_the_q1_restatement,
    )
    assert unordered is not None
    assert unordered.data["revenue"] != 111
    assert unordered.availability_time <= before_the_q1_restatement


def test_no_knowable_row_returns_none_not_a_fallback(
    connection: Connection, out_of_order_filings: UUID
):
    """None means "nothing was knowable", never "here is the closest thing"."""
    row = select_latest_as_of(
        connection,
        canonical_fundamentals,
        key=_key(out_of_order_filings),
        as_of=datetime(2000, 1, 1, tzinfo=UTC),
        precedence=("fiscal_period_end",),
    )
    assert row is None


def test_an_empty_precedence_is_the_documented_default(
    connection: Connection, out_of_order_filings: UUID
):
    """Passing `precedence=()` explicitly must equal omitting it.

    Pins the claim in the docstring that the default leaves emitted SQL
    unchanged for the callers that never pass it.
    """
    omitted = select_latest_as_of(
        connection,
        canonical_fundamentals,
        key=_key(out_of_order_filings),
        as_of=AFTER_EVERYTHING,
    )
    explicit = select_latest_as_of(
        connection,
        canonical_fundamentals,
        key=_key(out_of_order_filings),
        as_of=AFTER_EVERYTHING,
        precedence=(),
    )
    assert omitted is not None and explicit is not None
    assert omitted.id == explicit.id
