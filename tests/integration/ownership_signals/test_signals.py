"""Both ownership signals against a real PostgreSQL.

The decisions are unit-tested against plain counts in
`tests/unit/ownership_signals/`. What only a real database can prove is
the half those tests take on trust: that the aggregate query counts
*distinct people* rather than rows, that it counts only open-market
purchases, and — the one that matters most — that neither query can see
something before it was disclosable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from core.data_validation.result import MissReason
from core.ownership_signals.config import OwnershipSignalConfig
from core.ownership_signals.insider import (
    assess_insider_batch,
    insider_purchase_counts,
    store_insider_signals,
)
from core.ownership_signals.institutional import (
    assess_institutional_batch,
    latest_two_quarters,
    store_institutional_signals,
)
from core.ownership_signals.orchestrator import run_daily_ownership_signals
from infra.db.schema.ownership_signals import insider_cluster_signals

SCAN_DATE = date(2026, 3, 10)
AS_OF = datetime(2026, 3, 10, 21, 0, tzinfo=UTC)
THRESHOLDS = OwnershipSignalConfig().thresholds

#: A Tuesday evening; with `scan_offset_hours` at 17 the due session is
#: Monday — the same instant Module 28's suite uses, for the same reason.
NOW = datetime(2026, 3, 10, 21, 0, tzinfo=UTC)
ORCHESTRATOR_SCAN_DATE = date(2026, 3, 9)


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _counts(connection, security_id):
    return insider_purchase_counts(
        connection,
        [security_id],
        scan_date=SCAN_DATE,
        as_of=AS_OF,
        thresholds=THRESHOLDS,
    ).get(security_id)


# --------------------------------------------------------------------------
# Insider clusters: distinct people, purchases only
# --------------------------------------------------------------------------


def test_a_security_with_no_insider_rows_is_absent_from_the_query(connection, register):
    """Absent, not zero — which is what makes the verdict undetermined
    rather than negative."""
    security_id = register("ZZNOINS")

    assert _counts(connection, security_id) is None

    signal = assess_insider_batch(connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF)[
        security_id
    ]
    assert signal.raised is None
    assert signal.unavailable is MissReason.NEVER_INGESTED


def test_two_people_buying_in_the_window_is_a_cluster(connection, register, add_trade):
    security_id = register("ZZCLUSTER")
    add_trade(security_id, traded_at=_at(SCAN_DATE - timedelta(days=3)), person="Jane Doe")
    add_trade(security_id, traded_at=_at(SCAN_DATE - timedelta(days=9)), person="John Roe")

    counts = _counts(connection, security_id)
    assert counts.distinct_buyers == 2
    assert counts.ever_ingested is True

    signal = assess_insider_batch(connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF)[
        security_id
    ]
    assert signal.raised is True


def test_one_person_buying_repeatedly_is_still_one_person(connection, register, add_trade):
    """The count the threshold reads is people, not transactions — proved
    against the real `COUNT(DISTINCT ...)` rather than against a stub."""
    security_id = register("ZZTRANCHE")
    for offset, quantity in ((2, 500), (5, 900), (8, 1500)):
        add_trade(
            security_id,
            traded_at=_at(SCAN_DATE - timedelta(days=offset)),
            person="Jane Doe",
            quantity=quantity,
        )

    counts = _counts(connection, security_id)

    assert counts.purchase_count == 3
    assert counts.distinct_buyers == 1
    assert (
        assess_insider_batch(connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF)[
            security_id
        ].raised
        is False
    )


def test_sales_and_grants_are_not_counted_as_buying(connection, register, add_trade):
    """Two people acquired stock, neither of them by choosing to buy it:
    one was granted it and one sold. The signal must read `False`."""
    security_id = register("ZZNOTBUYS")
    add_trade(security_id, traded_at=_at(SCAN_DATE - timedelta(days=2)), person="Jane", code="A")
    add_trade(security_id, traded_at=_at(SCAN_DATE - timedelta(days=4)), person="John", code="S")
    add_trade(security_id, traded_at=_at(SCAN_DATE - timedelta(days=6)), person="Ada", code="M")

    counts = _counts(connection, security_id)

    assert counts.distinct_buyers == 0
    assert counts.ever_ingested is True  # data exists, so the verdict is real
    assert (
        assess_insider_batch(connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF)[
            security_id
        ].raised
        is False
    )


def test_purchases_older_than_the_window_do_not_count(connection, register, add_trade):
    security_id = register("ZZOLDBUYS")
    outside = SCAN_DATE - timedelta(days=THRESHOLDS.cluster_window_days + 5)
    add_trade(security_id, traded_at=_at(outside), person="Jane")
    add_trade(security_id, traded_at=_at(outside - timedelta(days=1)), person="John")

    counts = _counts(connection, security_id)

    assert counts.distinct_buyers == 0
    # But the security is still "ever ingested", so the answer is a
    # measured False rather than undetermined.
    assert counts.ever_ingested is True


def test_a_purchase_still_inside_its_disclosure_window_is_invisible(
    connection, register, add_trade
):
    """The leak this signal could most easily have had.

    Form 4 is due two business days after the trade. A purchase made
    today is not public today, and a cutoff before its disclosure must
    not see it — otherwise a replay reads insider buying before the
    market could have.
    """
    security_id = register("ZZUNDISCLOSED")
    add_trade(
        security_id,
        traded_at=_at(SCAN_DATE),
        person="Jane Doe",
        available=AS_OF + timedelta(hours=1),
    )

    # Nothing knowable at this cutoff at all — not even "ever ingested".
    assert _counts(connection, security_id) is None

    # And the same row is visible once its disclosure lag has passed.
    later = insider_purchase_counts(
        connection,
        [security_id],
        scan_date=SCAN_DATE,
        as_of=AS_OF + timedelta(days=3),
        thresholds=THRESHOLDS,
    )
    assert later[security_id].distinct_buyers == 1


def test_yesterdays_purchase_is_not_disclosed_yet(connection, register, add_trade):
    """A consequence of the Form 4 lag worth pinning on its own.

    A purchase made yesterday is *not* part of today's reading: its
    two-day filing window has not closed, so nothing about it was public
    at this cutoff. This is the correct answer and a slightly surprising
    one — the window looks back thirty days, but its recent edge is
    blunted by the disclosure lag, and a reader comparing this signal to
    a news feed should know that.
    """
    security_id = register("ZZYESTERDAY")
    add_trade(security_id, traded_at=_at(SCAN_DATE - timedelta(days=1)), person="Jane")
    add_trade(security_id, traded_at=_at(SCAN_DATE - timedelta(days=4)), person="John")

    counts = _counts(connection, security_id)

    # Only John's, four days back, has cleared its filing deadline.
    assert counts.distinct_buyers == 1


def test_the_reading_round_trips_through_storage(connection, register, add_trade):
    security_id = register("ZZSTOREINS")
    # Both far enough back that their Form 4 windows have closed — see
    # `test_yesterdays_purchase_is_not_disclosed_yet`.
    add_trade(security_id, traded_at=_at(SCAN_DATE - timedelta(days=4)), person="Jane")
    add_trade(security_id, traded_at=_at(SCAN_DATE - timedelta(days=6)), person="John")

    signals = assess_insider_batch(connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF)
    assert store_insider_signals(connection, list(signals.values())) == 1

    from sqlalchemy import select

    row = connection.execute(
        select(insider_cluster_signals).where(insider_cluster_signals.c.security_id == security_id)
    ).one()
    assert row.raised is True
    assert row.distinct_purchasers == 2
    assert row.window_days == THRESHOLDS.cluster_window_days
    assert row.unavailable_reason is None


def test_a_same_day_rerun_overwrites_rather_than_duplicating(connection, register, add_trade):
    security_id = register("ZZINSRERUN")
    add_trade(security_id, traded_at=_at(SCAN_DATE - timedelta(days=1)), person="Jane")

    for _ in range(2):
        signals = assess_insider_batch(connection, [security_id], scan_date=SCAN_DATE, as_of=AS_OF)
        store_insider_signals(connection, list(signals.values()))

    from sqlalchemy import select

    rows = connection.execute(
        select(insider_cluster_signals).where(insider_cluster_signals.c.security_id == security_id)
    ).all()
    assert len(rows) == 1


# --------------------------------------------------------------------------
# 13F: two quarters, and the 45-day rule
# --------------------------------------------------------------------------


def test_the_two_most_recent_knowable_quarters_come_back_newest_first(
    connection, register, add_quarter
):
    """Three quarters stored, two returned — and *which* two is the point.

    At a March 2026 cutoff, Q1 2026 has not even closed, so it is not a
    knowable quarter however complete its row is now. The answer is the
    two most recent quarters whose filing deadlines have passed, newest
    first.
    """
    security_id = register("ZZ13F")
    add_quarter(security_id, year=2025, quarter=2, investors=180, shares=700_000)
    add_quarter(security_id, year=2025, quarter=3, investors=200, shares=800_000)
    add_quarter(security_id, year=2025, quarter=4, investors=250, shares=900_000)
    # Stored, but its quarter has not closed at AS_OF — and even once it
    # does, 45 more days have to pass before anyone could read it.
    add_quarter(security_id, year=2026, quarter=1, investors=280, shares=1_000_000)

    periods = latest_two_quarters(connection, [security_id], as_of=AS_OF)[security_id]

    assert [period.period.as_tuple() for period in periods] == [(2025, 4), (2025, 3)]
    assert periods[0].investors_holding == 250
    assert periods[0].total_shares == Decimal("900000")


def test_a_quarter_inside_its_filing_window_is_not_visible_yet(connection, register, add_quarter):
    """The 45-day rule, proved against the real query.

    Q4 2025 closed on 31 December; managers have until mid-February to
    file. A cutoff in January must see Q3's figures, not Q4's — treating
    the quarter end as the knowable date would hand six weeks of
    hindsight on who was accumulating.
    """
    security_id = register("ZZ13FLAG")
    add_quarter(security_id, year=2025, quarter=3, investors=200)
    add_quarter(security_id, year=2025, quarter=4, investors=250)

    january = datetime(2026, 1, 15, tzinfo=UTC)
    periods = latest_two_quarters(connection, [security_id], as_of=january)[security_id]

    assert [period.period.as_tuple() for period in periods] == [(2025, 3)]

    # And Q4 becomes visible once its deadline has passed.
    march = datetime(2026, 3, 1, tzinfo=UTC)
    later = latest_two_quarters(connection, [security_id], as_of=march)[security_id]
    assert later[0].period.as_tuple() == (2025, 4)


def test_the_trend_is_computed_and_stored_from_two_quarters(connection, register, add_quarter):
    security_id = register("ZZ13FTREND")
    add_quarter(security_id, year=2025, quarter=3, investors=200, shares=1_000_000)
    add_quarter(security_id, year=2025, quarter=4, investors=250, shares=1_250_000)

    trends = assess_institutional_batch(connection, [security_id], as_of=AS_OF)
    trend = trends[security_id]

    assert trend.period.as_tuple() == (2025, 4)
    assert trend.investors_holding_change == 50
    assert trend.total_shares_change_percent == Decimal("25")
    assert store_institutional_signals(connection, [trend]) == 1


def test_a_security_with_no_quarters_is_never_ingested_and_stores_nothing(connection, register):
    """No quarter means no key to store a row under, and a missing row
    reads as exactly the same "nothing known" the row would have said."""
    security_id = register("ZZ13FNONE")

    trend = assess_institutional_batch(connection, [security_id], as_of=AS_OF)[security_id]

    assert trend.unavailable is MissReason.NEVER_INGESTED
    assert store_institutional_signals(connection, [trend]) == 0


# --------------------------------------------------------------------------
# The daily run
# --------------------------------------------------------------------------


def test_a_run_stores_an_insider_reading_for_every_universe_member(engine, universe):
    version_id, identities = universe(("ZZRUNA", "ZZRUNB"))

    report = run_daily_ownership_signals(engine, universe_version_id=version_id, now=NOW)

    assert report.scan_date == ORCHESTRATOR_SCAN_DATE
    assert report.universe_size == 2
    assert report.insider_stored == 2
    assert report.insider_undetermined == 2  # no Form 4 data ingested for either
    # No 13F data either, so nothing to key a trend row on.
    assert report.institutional_stored == 0
    assert report.institutional_undetermined == 2
    assert report.healthy is True


def test_a_run_with_an_empty_universe_is_healthy(engine, universe):
    version_id, _identities = universe(())

    report = run_daily_ownership_signals(engine, universe_version_id=version_id, now=NOW)

    assert report.universe_size == 0
    assert report.insider_stored == 0
    assert report.healthy is True


def test_the_run_report_serializes_to_a_complete_dict(engine, universe):
    version_id, _identities = universe(())

    payload = run_daily_ownership_signals(engine, universe_version_id=version_id, now=NOW).as_dict()

    assert payload["scan_date"] == ORCHESTRATOR_SCAN_DATE.isoformat()
    assert set(payload) == {
        "scan_date",
        "universe_size",
        "insider_raised",
        "insider_not_raised",
        "insider_undetermined",
        "insider_stored",
        "institutional_stored",
        "institutional_undetermined",
        "skipped_reason",
        "healthy",
    }
