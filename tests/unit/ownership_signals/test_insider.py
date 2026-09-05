"""The insider-cluster decision, in isolation.

`evaluate_insider_cluster` takes counts rather than a connection, for the
reason `core/news_signals/signal.py` gives: the three-state logic is
exactly as easy to get wrong as tier due-ness was, and deserves a test
that needs no database at all. The query that produces those counts —
including the PIT filter that keeps an undisclosed Form 4 out of an
earlier `as_of` — is tested against a real PostgreSQL in
`tests/integration/ownership_signals/`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from core.data_validation.result import MissReason
from core.ownership_signals.config import (
    CALIBRATABLE,
    OPEN_MARKET_PURCHASE_CODE,
    OwnershipThreshold,
    OwnershipThresholds,
)
from core.ownership_signals.insider import InsiderCluster, evaluate_insider_cluster

SCAN_DATE = date(2026, 3, 10)
AS_OF = datetime(2026, 3, 10, 21, 0, tzinfo=UTC)


@pytest.fixture
def thresholds() -> OwnershipThresholds:
    return OwnershipThresholds()


def _evaluate(cluster: InsiderCluster | None, thresholds: OwnershipThresholds):
    return evaluate_insider_cluster(
        security_id=uuid4(),
        scan_date=SCAN_DATE,
        as_of=AS_OF,
        cluster=cluster,
        thresholds=thresholds,
        config_version_label="test-version",
    )


# --------------------------------------------------------------------------
# Undetermined: never measured is not "not buying"
# --------------------------------------------------------------------------


def test_a_security_with_no_insider_history_is_undetermined_not_false(thresholds):
    """The distinction this signal turns on. `False` would claim ARGUS
    looked at this security's Form 4 filings and found no cluster, when
    the truth is that it has never fetched any."""
    signal = _evaluate(None, thresholds)

    assert signal.raised is None
    assert signal.determined is False
    assert signal.unavailable is MissReason.NEVER_INGESTED


def test_a_row_group_that_reports_no_ingested_rows_is_also_undetermined(thresholds):
    """The same case arriving the other way: a group with `ever_ingested`
    false, which is what a security whose only filings are still inside
    their two-day disclosure window looks like at this cutoff."""
    signal = _evaluate(
        InsiderCluster(distinct_buyers=0, purchase_count=0, ever_ingested=False), thresholds
    )

    assert signal.raised is None
    assert signal.unavailable is MissReason.NEVER_INGESTED


# --------------------------------------------------------------------------
# Determined: the cluster decision
# --------------------------------------------------------------------------


def test_history_with_no_purchases_in_the_window_is_a_measured_false(thresholds):
    """Data exists and was read; there simply is no cluster. That is a
    real answer, and distinct from the undetermined case above."""
    signal = _evaluate(
        InsiderCluster(distinct_buyers=0, purchase_count=0, ever_ingested=True), thresholds
    )

    assert signal.raised is False
    assert signal.determined is True
    assert signal.unavailable is None


def test_one_buyer_is_not_a_cluster(thresholds):
    """A single insider's purchase can be personal liquidity or a
    diversification schedule — the reason the bankruptcy gate likewise
    requires two independent signals rather than one."""
    signal = _evaluate(
        InsiderCluster(distinct_buyers=1, purchase_count=4, ever_ingested=True), thresholds
    )

    assert signal.raised is False
    assert signal.distinct_buyers == 1


def test_the_minimum_number_of_distinct_buyers_raises(thresholds):
    """`>=`, not `>`: exactly at the threshold is a cluster."""
    signal = _evaluate(
        InsiderCluster(distinct_buyers=thresholds.min_buyers, purchase_count=2, ever_ingested=True),
        thresholds,
    )

    assert signal.raised is True


def test_more_than_the_minimum_raises(thresholds):
    signal = _evaluate(
        InsiderCluster(distinct_buyers=5, purchase_count=9, ever_ingested=True), thresholds
    )

    assert signal.raised is True
    assert signal.distinct_buyers == 5


def test_many_purchases_by_one_person_are_still_one_person(thresholds):
    """The count that matters is people, not transactions: an officer
    buying in three tranches decided once, and letting the tranches clear
    a threshold meant to require agreement would defeat the point."""
    signal = _evaluate(
        InsiderCluster(distinct_buyers=1, purchase_count=12, ever_ingested=True), thresholds
    )

    assert signal.raised is False
    assert signal.detail["purchase_count"] == 12
    assert signal.detail["distinct_buyers"] == 1


# --------------------------------------------------------------------------
# The configuration travels with the verdict
# --------------------------------------------------------------------------


def test_the_threshold_is_read_from_configuration_not_hardcoded(thresholds):
    """Move the bar and the verdict moves — the same discipline every
    other signal module in this project is held to."""
    cluster = InsiderCluster(distinct_buyers=2, purchase_count=2, ever_ingested=True)

    strict = OwnershipThresholds(
        insider_cluster_min_buyers=OwnershipThreshold(
            value=3.0, kind=CALIBRATABLE, rationale="test"
        )
    )

    assert _evaluate(cluster, thresholds).raised is True
    assert _evaluate(cluster, strict).raised is False


def test_a_stored_verdict_carries_the_thresholds_it_applied(thresholds):
    signal = _evaluate(
        InsiderCluster(distinct_buyers=2, purchase_count=2, ever_ingested=True), thresholds
    )

    assert signal.window_days == thresholds.cluster_window_days
    assert signal.min_buyers == thresholds.min_buyers
    assert signal.config_version_label == "test-version"


def test_the_detail_names_which_transaction_code_was_counted(thresholds):
    """A reader of a stored row should not have to open the source to
    learn that grants and option exercises were excluded."""
    signal = _evaluate(
        InsiderCluster(distinct_buyers=2, purchase_count=2, ever_ingested=True), thresholds
    )

    assert signal.detail["transaction_code_counted"] == OPEN_MARKET_PURCHASE_CODE
    assert OPEN_MARKET_PURCHASE_CODE == "P"


def test_an_undetermined_signal_always_names_why_and_reports_no_count(thresholds):
    signal = _evaluate(None, thresholds)

    assert signal.unavailable is not None
    assert signal.distinct_buyers == 0
    assert "reason" in signal.detail


def test_a_determined_signal_never_carries_a_miss_reason(thresholds):
    """The converse: a real verdict must not be filterable out as if it
    were absent evidence."""
    for buyers in (0, 1, 2, 7):
        signal = _evaluate(
            InsiderCluster(distinct_buyers=buyers, purchase_count=buyers, ever_ingested=True),
            thresholds,
        )
        assert signal.raised is not None
        assert signal.unavailable is None


def test_the_serialized_form_round_trips_every_field(thresholds):
    payload = _evaluate(
        InsiderCluster(distinct_buyers=2, purchase_count=3, ever_ingested=True), thresholds
    ).as_dict()

    assert payload["raised"] is True
    assert payload["distinct_buyers"] == 2
    assert payload["unavailable"] is None
    assert payload["scan_date"] == SCAN_DATE.isoformat()
