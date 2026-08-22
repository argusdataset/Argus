"""Listing intervals and admission rules.

The survivorship-bias property lives here: a historical universe must
include securities that have since been delisted, which means membership
has to be a dated interval rather than a status flag.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from core.universe.admission import (
    AdmissionReport,
    ExclusionReason,
    admits,
    has_foreign_suffix,
)
from core.universe.intervals import (
    IntervalEvidence,
    ListingObservation,
    build_intervals,
    intervals_covering,
)
from core.universe.repository import membership_checksum
from data.canonical_model.exchanges import CanonicalExchange
from infra.db.enums import ListingStatus

OBSERVED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def moment(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def listed(security_id, symbol="AAPL", exchange=CanonicalExchange.NASDAQ) -> ListingObservation:
    return ListingObservation(
        security_id=security_id, symbol=symbol, exchange=exchange, is_delisted=False
    )


def delisted(
    security_id,
    symbol="LEHMQ",
    *,
    ipo: date | None = date(1994, 5, 6),
    gone: date | None = date(2008, 9, 17),
    exchange=CanonicalExchange.NYSE,
) -> ListingObservation:
    return ListingObservation(
        security_id=security_id,
        symbol=symbol,
        exchange=exchange,
        ipo_date=ipo,
        delisted_date=gone,
        is_delisted=True,
    )


# --------------------------------------------------------------------------
# Survivorship bias: the whole point of the module
# --------------------------------------------------------------------------


def test_a_delisted_security_is_in_the_universe_for_dates_it_was_listed():
    """The core survivorship-bias-resistance property."""
    security_id = uuid4()
    intervals = build_intervals([delisted(security_id)], observed_at=OBSERVED_AT)

    covering = intervals_covering(intervals, moment(2005))

    assert security_id in covering
    assert covering[security_id].listing_status is ListingStatus.DELISTED


def test_a_delisted_security_is_absent_from_todays_universe():
    security_id = uuid4()
    intervals = build_intervals([delisted(security_id)], observed_at=OBSERVED_AT)

    assert security_id not in intervals_covering(intervals, OBSERVED_AT)


def test_a_delisted_security_is_absent_before_it_listed():
    security_id = uuid4()
    intervals = build_intervals([delisted(security_id)], observed_at=OBSERVED_AT)

    assert security_id not in intervals_covering(intervals, moment(1990))


def test_the_delisting_date_is_exclusive():
    """Half-open: a security is not listed on the day it delists."""
    security_id = uuid4()
    intervals = build_intervals([delisted(security_id)], observed_at=OBSERVED_AT)

    assert intervals[0].covers(moment(2008, 9, 16))
    assert not intervals[0].covers(moment(2008, 9, 17))


def test_a_historical_universe_contains_both_survivors_and_casualties():
    """The comparison that makes the bias concrete."""
    survivor, casualty = uuid4(), uuid4()
    intervals = build_intervals(
        [listed(survivor, "AAPL"), delisted(casualty, "LEHMQ")],
        observed_at=OBSERVED_AT,
        first_bar_dates={survivor: date(1980, 12, 12)},
    )

    historical = intervals_covering(intervals, moment(2005))
    today = intervals_covering(intervals, OBSERVED_AT)

    assert {survivor, casualty} == set(historical)
    assert set(today) == {survivor}


# --------------------------------------------------------------------------
# Evidence for interval boundaries
# --------------------------------------------------------------------------


def test_delisted_feed_dates_are_recorded_as_reported_evidence():
    intervals = build_intervals([delisted(uuid4())], observed_at=OBSERVED_AT)

    assert intervals[0].from_evidence is IntervalEvidence.DELISTED_FEED
    assert intervals[0].to_evidence is IntervalEvidence.DELISTED_FEED


def test_price_history_dates_a_currently_listed_security():
    """FMP's stock-list has no IPO date, so the first bar is the evidence."""
    security_id = uuid4()
    intervals = build_intervals(
        [listed(security_id)],
        observed_at=OBSERVED_AT,
        first_bar_dates={security_id: date(1980, 12, 12)},
    )

    assert intervals[0].listed_from == moment(1980, 12, 12)
    assert intervals[0].from_evidence is IntervalEvidence.PRICE_HISTORY
    assert intervals[0].listed_to is None


def test_without_price_history_a_listing_only_claims_from_first_observation():
    """Errs narrow: ARGUS claims listing only over what it can evidence."""
    security_id = uuid4()
    intervals = build_intervals([listed(security_id)], observed_at=OBSERVED_AT)

    assert intervals[0].listed_from == OBSERVED_AT
    assert intervals[0].from_evidence is IntervalEvidence.FIRST_OBSERVED
    # And so it is correctly absent from a historical universe rather than
    # being assumed to have existed.
    assert security_id not in intervals_covering(intervals, moment(2005))


def test_a_delisting_with_no_ipo_date_falls_back_to_price_history():
    security_id = uuid4()
    intervals = build_intervals(
        [delisted(security_id, ipo=None)],
        observed_at=OBSERVED_AT,
        first_bar_dates={security_id: date(1996, 3, 1)},
    )

    assert intervals[0].listed_from == moment(1996, 3, 1)
    assert intervals[0].from_evidence is IntervalEvidence.PRICE_HISTORY


def test_a_delisting_with_no_dates_at_all_is_marked_missing_not_guessed():
    security_id = uuid4()
    intervals = build_intervals(
        [delisted(security_id, ipo=None, gone=None)], observed_at=OBSERVED_AT
    )

    assert intervals[0].from_evidence is IntervalEvidence.MISSING
    assert intervals[0].to_evidence is IntervalEvidence.MISSING


def test_an_interval_is_never_degenerate():
    """The database's ordering check would reject listed_to <= listed_from."""
    security_id = uuid4()
    intervals = build_intervals(
        [delisted(security_id, ipo=date(2008, 9, 17), gone=date(2008, 9, 17))],
        observed_at=OBSERVED_AT,
    )

    assert intervals[0].listed_to > intervals[0].listed_from


def test_evidence_label_records_both_boundaries():
    intervals = build_intervals([delisted(uuid4())], observed_at=OBSERVED_AT)

    assert intervals[0].evidence_label == "from=delisted_feed;to=delisted_feed"


# --------------------------------------------------------------------------
# Edge case: delisted, then relisted
# --------------------------------------------------------------------------


def test_a_relisted_security_gets_two_intervals_with_the_gap_preserved():
    """Claiming continuous listing across the gap would be a silent error."""
    security_id = uuid4()
    intervals = build_intervals(
        [
            delisted(security_id, "RELIST", ipo=date(1999, 1, 1), gone=date(2009, 1, 1)),
            listed(security_id, "RELIST", exchange=CanonicalExchange.NYSE),
        ],
        observed_at=OBSERVED_AT,
    )

    assert len(intervals) == 2
    # Listed in its first era...
    assert security_id in intervals_covering(intervals, moment(2005))
    # ...not during the gap...
    assert security_id not in intervals_covering(intervals, moment(2012))
    # ...and listed again now.
    assert security_id in intervals_covering(intervals, OBSERVED_AT)


def test_a_relisted_securitys_current_interval_cannot_start_before_it_returned():
    """Even when price history reaches further back than the delisting."""
    security_id = uuid4()
    intervals = build_intervals(
        [
            delisted(security_id, "RELIST", ipo=date(1999, 1, 1), gone=date(2009, 1, 1)),
            listed(security_id, "RELIST"),
        ],
        observed_at=OBSERVED_AT,
        # Bars from the first era would otherwise date the second interval.
        first_bar_dates={security_id: date(1999, 1, 1)},
    )

    current = next(item for item in intervals if item.listed_to is None)
    assert current.listed_from >= moment(2009, 1, 1)
    assert current.from_evidence is IntervalEvidence.MISSING


def test_overlapping_intervals_resolve_to_the_most_recent():
    """universe_membership permits one row per security per version."""
    security_id = uuid4()
    intervals = build_intervals(
        [
            delisted(security_id, "DUP", ipo=date(1999, 1, 1), gone=date(2020, 1, 1)),
            listed(security_id, "DUP"),
        ],
        observed_at=OBSERVED_AT,
        first_bar_dates={security_id: date(1999, 1, 1)},
    )

    covering = intervals_covering(intervals, OBSERVED_AT)
    assert len(covering) == 1


# --------------------------------------------------------------------------
# Ticker changes join by identity
# --------------------------------------------------------------------------


def test_a_ticker_change_yields_one_membership_record_not_two():
    """FB and META are one security; joining on security_id is what does it."""
    security_id = uuid4()
    intervals = build_intervals(
        [listed(security_id, "META"), listed(security_id, "FB")],
        observed_at=OBSERVED_AT,
        first_bar_dates={security_id: date(2012, 5, 18)},
    )

    covering = intervals_covering(intervals, OBSERVED_AT)
    assert len(covering) == 1
    assert next(iter(covering)) == security_id


def test_two_different_securities_stay_separate():
    first, second = uuid4(), uuid4()
    intervals = build_intervals(
        [listed(first, "AAPL"), listed(second, "MSFT")], observed_at=OBSERVED_AT
    )

    assert len(intervals_covering(intervals, OBSERVED_AT)) == 2


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


@pytest.mark.parametrize("exchange", [CanonicalExchange.NYSE, CanonicalExchange.NASDAQ])
def test_nyse_and_nasdaq_are_admitted(exchange):
    assert admits(exchange, "AAPL") is None


@pytest.mark.parametrize(
    "exchange",
    [CanonicalExchange.NYSE_ARCA, CanonicalExchange.NYSE_AMERICAN, CanonicalExchange.OTC],
)
def test_other_recognised_venues_are_excluded_with_a_reason(exchange):
    assert admits(exchange, "SPY") is ExclusionReason.NON_UNIVERSE_EXCHANGE


def test_unknown_exchange_is_its_own_exclusion_reason():
    """Distinct from 'not our venue' — an unrecognised label is a warning sign."""
    assert admits(CanonicalExchange.UNKNOWN, "XYZ") is ExclusionReason.UNKNOWN_EXCHANGE


def test_a_foreign_suffix_overrides_a_us_exchange_label():
    """A provider row whose label and ticker disagree is a data error."""
    assert admits(CanonicalExchange.NASDAQ, "SHOP.TO") is ExclusionReason.FOREIGN_SYMBOL_SUFFIX


def test_us_class_share_tickers_are_not_treated_as_foreign():
    """Blanket-excluding dotted tickers would drop real NYSE constituents."""
    assert not has_foreign_suffix("BRK.B")
    assert not has_foreign_suffix("BF.B")
    assert admits(CanonicalExchange.NYSE, "BRK.B") is None


def test_known_foreign_suffixes_are_detected():
    for symbol in ("SHOP.TO", "VOD.L", "SAP.DE", "0700.HK"):
        assert has_foreign_suffix(symbol), symbol


def test_missing_symbol_is_excluded():
    assert admits(CanonicalExchange.NASDAQ, "") is ExclusionReason.MISSING_SYMBOL


# --------------------------------------------------------------------------
# Reporting: never drop silently
# --------------------------------------------------------------------------


def test_unknown_exchange_securities_are_counted_not_dropped():
    """Module 05's instruction, carried forward."""
    report = AdmissionReport()
    report.admit(CanonicalExchange.NASDAQ)
    report.exclude("XYZ", ExclusionReason.UNKNOWN_EXCHANGE, exchange_label="Bucharest")
    report.exclude("ABC", ExclusionReason.UNKNOWN_EXCHANGE, exchange_label="Bucharest")

    assert report.unknown_exchange_count == 2
    assert report.admitted == 1
    assert report.excluded == 2


def test_unrecognised_venue_labels_are_recorded_for_diagnosis():
    """The list to read when the universe shrinks unexpectedly."""
    report = AdmissionReport()
    report.exclude("A", ExclusionReason.UNKNOWN_EXCHANGE, exchange_label="NASDAQ Renamed Tier")
    report.exclude("B", ExclusionReason.UNKNOWN_EXCHANGE, exchange_label="NASDAQ Renamed Tier")

    assert report.unknown_exchange_labels["NASDAQ Renamed Tier"] == 2
    assert "NASDAQ Renamed Tier" in report.summary()["unknown_exchange_labels"]


def test_exclusion_samples_are_bounded():
    report = AdmissionReport(sample_limit=3)
    for index in range(50):
        report.exclude(f"SYM{index}", ExclusionReason.NON_UNIVERSE_EXCHANGE)

    assert report.excluded_by_reason[ExclusionReason.NON_UNIVERSE_EXCHANGE] == 50
    assert len(report.samples[ExclusionReason.NON_UNIVERSE_EXCHANGE]) == 3


def test_summary_is_json_serializable():
    import json

    report = AdmissionReport()
    report.admit(CanonicalExchange.NYSE)
    report.exclude("X", ExclusionReason.UNKNOWN_EXCHANGE, exchange_label="Unknown Venue")

    assert json.loads(json.dumps(report.summary()))["admitted"] == 1


# --------------------------------------------------------------------------
# Version checksums
# --------------------------------------------------------------------------


def test_identical_membership_produces_an_identical_checksum():
    """A re-run over unchanged data must not create a second version."""
    security_id = uuid4()
    first = build_intervals([delisted(security_id)], observed_at=OBSERVED_AT)
    second = build_intervals([delisted(security_id)], observed_at=OBSERVED_AT)

    assert membership_checksum(first) == membership_checksum(second)


def test_checksum_is_order_independent():
    a, b = uuid4(), uuid4()
    forward = build_intervals([listed(a, "AAA"), listed(b, "BBB")], observed_at=OBSERVED_AT)
    reverse = build_intervals([listed(b, "BBB"), listed(a, "AAA")], observed_at=OBSERVED_AT)

    assert membership_checksum(forward) == membership_checksum(reverse)


def test_a_changed_interval_changes_the_checksum():
    security_id = uuid4()
    original = build_intervals([delisted(security_id)], observed_at=OBSERVED_AT)
    corrected = build_intervals(
        [delisted(security_id, gone=date(2008, 9, 20))], observed_at=OBSERVED_AT
    )

    assert membership_checksum(original) != membership_checksum(corrected)
