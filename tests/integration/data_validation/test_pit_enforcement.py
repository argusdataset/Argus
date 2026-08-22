"""The adversarial future-leakage test — this module's actual point.

Everything else in Module 07's test suite is worth having. This file is
worth having *more than the rest combined*: if the enforcement here can
be defeated, no other test in the project means anything, because every
downstream signal, feature, and backtest result assumes this holds.

The scenario is concrete, not hypothetical: a fundamentals figure gets
restated months after its original filing (routine — companies correct
their own numbers). A query made *before* the restatement must see only
the original value. A query made *after* must see the correction. Getting
this backwards — even once, even quietly — is precisely how ARGUS's core
claim ("this pattern would have worked historically") becomes false in a
way that looks like a perfectly ordinary, plausible number.

`test_a_naive_query_would_leak_the_restatement` is not decoration: it
constructs the exact wrong query — the one a less careful implementation
would plausibly have written — against the same fixture data, and proves
it produces the leak. Read side by side with
`test_the_real_query_blocks_the_leak`, the pair is the whole argument for
why `get_fundamental_as_of` is built the way it is.

I additionally verified this by hand during development, in two steps.
First attempt: swapping `get_fundamental_as_of`'s `ORDER BY
availability_time DESC` for `ORDER BY observation_time DESC` while
leaving the `availability_time <= as_of` WHERE filter in place —
`test_the_real_query_blocks_the_leak` still passed, because this
fixture's `observation_time` and `availability_time` happen to coincide,
so the WHERE clause alone was still doing all the work and the ORDER BY
was never the deciding factor. Worth recording rather than quietly
discarding: it's a reminder that "I changed something and the test still
passed" is not the same as "the enforcement is provably load-bearing" —
you have to break the actual mechanism the test claims to protect.
Second attempt did that: removing the `availability_time <= as_of`
condition from the WHERE clause entirely. That failed
`test_the_real_query_blocks_the_leak` immediately, returning the restated
$91.2B figure for a query dated two months before the restatement
existed — exactly the leak this test exists to catch. Reverted afterward;
`fundamentals.py` is unmodified from what a plain diff shows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.data_validation.entities import EntityType
from core.data_validation.fundamentals import get_fundamental_as_of, get_latest_fundamental_as_of
from core.data_validation.ohlcv import get_ohlcv_bar_as_of
from core.data_validation.query import get_as_of
from core.data_validation.result import MissReason
from data.canonical_model.pit import session_close
from data.canonical_model.records import CanonicalStatementType, CanonicalTimeframe
from infra.db.schema.canonical import canonical_fundamentals, canonical_ohlcv

FISCAL_PERIOD_END = datetime(2019, 3, 31, tzinfo=UTC)
ORIGINAL_ACCEPTED = datetime(2019, 5, 15, 18, 8, 27, tzinfo=UTC)
ORIGINAL_AVAILABLE = ORIGINAL_ACCEPTED  # no artificial lag in this fixture
RESTATEMENT_ACCEPTED = datetime(2019, 8, 1, 9, 0, 0, tzinfo=UTC)
RESTATEMENT_AVAILABLE = RESTATEMENT_ACCEPTED

ORIGINAL_REVENUE = 90_753_000_000
RESTATED_REVENUE = 91_200_000_000  # a correction filed months later


def _insert_fundamental(
    connection: Connection,
    security_id: UUID,
    *,
    observation_time: datetime,
    availability_time: datetime,
    revenue: int,
) -> None:
    connection.execute(
        canonical_fundamentals.insert().values(
            security_id=security_id,
            statement_type=CanonicalStatementType.INCOME_STATEMENT.value,
            fiscal_period="Q1",
            fiscal_period_end=FISCAL_PERIOD_END,
            event_time=FISCAL_PERIOD_END,
            observation_time=observation_time,
            availability_time=availability_time,
            ingestion_time=availability_time,
            data={"revenue": revenue},
        )
    )


@pytest.fixture
def restated_fundamental(connection: Connection, security_id: UUID) -> UUID:
    """Two rows for the same (security, statement_type, period): a restatement.

    Original filing accepted May 15 2019, revenue $90.753B. Restated
    August 1 2019, revenue revised to $91.2B. Both rows persist — Module
    05's rule that a restatement is a new row, never an edit — so this is
    exactly the shape of data the enforcement has to get right.
    """
    _insert_fundamental(
        connection,
        security_id,
        observation_time=ORIGINAL_ACCEPTED,
        availability_time=ORIGINAL_AVAILABLE,
        revenue=ORIGINAL_REVENUE,
    )
    _insert_fundamental(
        connection,
        security_id,
        observation_time=RESTATEMENT_ACCEPTED,
        availability_time=RESTATEMENT_AVAILABLE,
        revenue=RESTATED_REVENUE,
    )
    return security_id


# --------------------------------------------------------------------------
# THE adversarial test
# --------------------------------------------------------------------------


def test_the_real_query_blocks_the_leak(connection: Connection, restated_fundamental: UUID):
    """A query dated before the restatement must not see it.

    This is the test. If it passes, the restated $91.2B figure — which
    did not exist until August — is provably invisible to a query dated
    two months earlier. If it fails, ARGUS's core claim is false for
    every backtest that ever touches this quarter.
    """
    as_of_before_restatement = datetime(2019, 6, 1, tzinfo=UTC)

    result = get_fundamental_as_of(
        connection,
        restated_fundamental,
        CanonicalStatementType.INCOME_STATEMENT,
        FISCAL_PERIOD_END.date(),
        as_of_before_restatement,
    )

    assert result.found
    assert result.unwrap().data["revenue"] == ORIGINAL_REVENUE
    assert result.unwrap().data["revenue"] != RESTATED_REVENUE


def test_the_real_query_sees_the_restatement_once_it_is_available(
    connection: Connection, restated_fundamental: UUID
):
    """The mirror image: after August, the corrected figure IS visible.

    Blocking the leak is not the same as blocking the data — once the
    restatement is actually knowable, a query must see it. A permanently
    stale answer would be just as wrong as a premature one.
    """
    as_of_after_restatement = datetime(2019, 9, 1, tzinfo=UTC)

    result = get_fundamental_as_of(
        connection,
        restated_fundamental,
        CanonicalStatementType.INCOME_STATEMENT,
        FISCAL_PERIOD_END.date(),
        as_of_after_restatement,
    )

    assert result.unwrap().data["revenue"] == RESTATED_REVENUE


def test_a_query_exactly_on_the_restatements_availability_time_sees_it(
    connection: Connection, restated_fundamental: UUID
):
    """The boundary is inclusive: availability_time <= as_of, not <."""
    result = get_fundamental_as_of(
        connection,
        restated_fundamental,
        CanonicalStatementType.INCOME_STATEMENT,
        FISCAL_PERIOD_END.date(),
        RESTATEMENT_AVAILABLE,
    )
    assert result.unwrap().data["revenue"] == RESTATED_REVENUE


def test_a_query_one_microsecond_before_availability_does_not_see_it(
    connection: Connection, restated_fundamental: UUID
):
    result = get_fundamental_as_of(
        connection,
        restated_fundamental,
        CanonicalStatementType.INCOME_STATEMENT,
        FISCAL_PERIOD_END.date(),
        RESTATEMENT_AVAILABLE - timedelta(microseconds=1),
    )
    assert result.unwrap().data["revenue"] == ORIGINAL_REVENUE


def test_a_naive_query_would_leak_the_restatement(
    connection: Connection, restated_fundamental: UUID
):
    """The wrong version, run against the same fixture, to make the danger concrete.

    A plausible mistake: order by `observation_time` (when the filing was
    accepted) instead of `availability_time` (when ARGUS could see it), or
    take the row with the greatest `event_time` regardless of when it was
    filed. Both are one-line changes from the correct query. This proves
    they leak — the real function in fundamentals.py does neither.
    """
    as_of_before_restatement = datetime(2019, 6, 1, tzinfo=UTC)

    # Mistake A: "most recent restatement of this record" with no as_of
    # filter at all — the bug of forgetting PIT filtering entirely.
    unconditionally_latest = connection.execute(
        select(canonical_fundamentals.c.data)
        .where(
            canonical_fundamentals.c.security_id == restated_fundamental,
            canonical_fundamentals.c.statement_type
            == CanonicalStatementType.INCOME_STATEMENT.value,
            canonical_fundamentals.c.fiscal_period_end == FISCAL_PERIOD_END,
        )
        .order_by(canonical_fundamentals.c.observation_time.desc())
        .limit(1)
    ).scalar_one()
    assert unconditionally_latest["revenue"] == RESTATED_REVENUE, (
        "fixture sanity check: the naive query should leak the future value"
    )

    # Mistake B: filtering on event_time (the fiscal period, which never
    # changes) instead of availability_time — filters nothing at all,
    # since every row shares the same event_time.
    filtered_on_event_time = connection.execute(
        select(canonical_fundamentals.c.data)
        .where(
            canonical_fundamentals.c.security_id == restated_fundamental,
            canonical_fundamentals.c.event_time <= as_of_before_restatement,
        )
        .order_by(canonical_fundamentals.c.observation_time.desc())
        .limit(1)
    ).scalar_one()
    assert filtered_on_event_time["revenue"] == RESTATED_REVENUE, (
        "fixture sanity check: filtering on event_time does not block the leak"
    )


def test_get_as_of_dispatcher_blocks_the_leak_too(
    connection: Connection, restated_fundamental: UUID
):
    """The single chokepoint gives the same protection as the typed function."""
    result = get_as_of(
        connection,
        EntityType.FUNDAMENTALS,
        restated_fundamental,
        datetime(2019, 6, 1, tzinfo=UTC),
        statement_type=CanonicalStatementType.INCOME_STATEMENT,
        fiscal_period_end=FISCAL_PERIOD_END.date(),
    )
    assert result.unwrap().data["revenue"] == ORIGINAL_REVENUE


# --------------------------------------------------------------------------
# Restatement handling: "latest known", not "latest filed"
# --------------------------------------------------------------------------


def test_latest_fundamental_as_of_respects_restatement_timing(
    connection: Connection, restated_fundamental: UUID
):
    before = get_latest_fundamental_as_of(
        connection,
        restated_fundamental,
        CanonicalStatementType.INCOME_STATEMENT,
        datetime(2019, 6, 1, tzinfo=UTC),
    )
    after = get_latest_fundamental_as_of(
        connection,
        restated_fundamental,
        CanonicalStatementType.INCOME_STATEMENT,
        datetime(2019, 9, 1, tzinfo=UTC),
    )
    assert before.unwrap().data["revenue"] == ORIGINAL_REVENUE
    assert after.unwrap().data["revenue"] == RESTATED_REVENUE


def test_latest_fundamental_as_of_picks_the_most_recent_period_not_just_freshest_row(
    connection: Connection, security_id: UUID
):
    """A later period, once known, outranks an earlier period's restatement."""
    q1_end = datetime(2019, 3, 31, tzinfo=UTC)
    q2_end = datetime(2019, 6, 30, tzinfo=UTC)

    connection.execute(
        canonical_fundamentals.insert().values(
            security_id=security_id,
            statement_type=CanonicalStatementType.INCOME_STATEMENT.value,
            fiscal_period="Q1",
            fiscal_period_end=q1_end,
            event_time=q1_end,
            observation_time=datetime(2019, 5, 15, tzinfo=UTC),
            availability_time=datetime(2019, 5, 15, tzinfo=UTC),
            ingestion_time=datetime(2019, 5, 15, tzinfo=UTC),
            data={"revenue": 100},
        )
    )
    connection.execute(
        canonical_fundamentals.insert().values(
            security_id=security_id,
            statement_type=CanonicalStatementType.INCOME_STATEMENT.value,
            fiscal_period="Q2",
            fiscal_period_end=q2_end,
            event_time=q2_end,
            observation_time=datetime(2019, 8, 15, tzinfo=UTC),
            availability_time=datetime(2019, 8, 15, tzinfo=UTC),
            ingestion_time=datetime(2019, 8, 15, tzinfo=UTC),
            data={"revenue": 200},
        )
    )

    result = get_latest_fundamental_as_of(
        connection,
        security_id,
        CanonicalStatementType.INCOME_STATEMENT,
        datetime(2019, 9, 1, tzinfo=UTC),
    )
    assert result.unwrap().data["revenue"] == 200
    assert result.unwrap().fiscal_period_end == q2_end.date()


def test_a_year_qualified_period_end_disambiguates_same_label_different_years(
    connection: Connection, security_id: UUID
):
    """A latent Module 03 near-miss this module works around at the query layer.

    `fiscal_period` alone ("Q1") does not distinguish different years.
    Querying by `fiscal_period_end` (an actual date) does — this asserts
    the 2019 Q1 row is never returned for a 2020 Q1 request just because
    both happen to be labelled "Q1".
    """
    for year, revenue in ((2019, 100), (2020, 200)):
        period_end = datetime(year, 3, 31, tzinfo=UTC)
        connection.execute(
            canonical_fundamentals.insert().values(
                security_id=security_id,
                statement_type=CanonicalStatementType.INCOME_STATEMENT.value,
                fiscal_period="Q1",  # deliberately identical label both years
                fiscal_period_end=period_end,
                event_time=period_end,
                observation_time=period_end,
                availability_time=period_end,
                ingestion_time=period_end,
                data={"revenue": revenue},
            )
        )

    result_2019 = get_fundamental_as_of(
        connection,
        security_id,
        CanonicalStatementType.INCOME_STATEMENT,
        datetime(2019, 3, 31, tzinfo=UTC).date(),
        datetime(2021, 1, 1, tzinfo=UTC),
    )
    result_2020 = get_fundamental_as_of(
        connection,
        security_id,
        CanonicalStatementType.INCOME_STATEMENT,
        datetime(2020, 3, 31, tzinfo=UTC).date(),
        datetime(2021, 1, 1, tzinfo=UTC),
    )
    assert result_2019.unwrap().data["revenue"] == 100
    assert result_2020.unwrap().data["revenue"] == 200


# --------------------------------------------------------------------------
# No implicit "current" fallback
# --------------------------------------------------------------------------


def test_a_period_with_no_data_yet_returns_an_explicit_miss_not_none(
    connection: Connection, security_id: UUID
):
    result = get_fundamental_as_of(
        connection,
        security_id,
        CanonicalStatementType.INCOME_STATEMENT,
        FISCAL_PERIOD_END.date(),
        datetime(2019, 1, 1, tzinfo=UTC),
    )
    assert not result
    assert result.reason is MissReason.NOT_YET_AVAILABLE


def test_a_miss_never_silently_substitutes_the_most_recent_row(
    connection: Connection, restated_fundamental: UUID
):
    """Querying long before either row exists must not fall back to 'whatever we have'."""
    result = get_fundamental_as_of(
        connection,
        restated_fundamental,
        CanonicalStatementType.INCOME_STATEMENT,
        FISCAL_PERIOD_END.date(),
        datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert not result
    assert result.value is None


# --------------------------------------------------------------------------
# OHLCV restatement handling (the same rule, a second entity)
# --------------------------------------------------------------------------


def test_ohlcv_restatement_is_also_pit_correct(connection: Connection, security_id: UUID):
    """A provider price correction must not leak early either."""
    bar_date = session_close(datetime(2024, 1, 3, tzinfo=UTC).date())
    first_seen = datetime(2024, 1, 3, 22, 0, tzinfo=UTC)
    corrected_seen = datetime(2024, 1, 10, 12, 0, tzinfo=UTC)

    for observed, close in ((first_seen, "184.25"), (corrected_seen, "184.50")):
        connection.execute(
            canonical_ohlcv.insert().values(
                security_id=security_id,
                timeframe=CanonicalTimeframe.DAILY.value,
                event_time=bar_date,
                observation_time=observed,
                availability_time=observed,
                ingestion_time=observed,
                open_raw=close,
                high_raw=close,
                low_raw=close,
                close_raw=close,
                volume_raw=1000,
            )
        )

    before = get_ohlcv_bar_as_of(
        connection, security_id, bar_date.date(), datetime(2024, 1, 5, tzinfo=UTC)
    )
    after = get_ohlcv_bar_as_of(
        connection, security_id, bar_date.date(), datetime(2024, 1, 15, tzinfo=UTC)
    )

    assert str(before.unwrap().close_raw) == "184.250000"
    assert str(after.unwrap().close_raw) == "184.500000"
