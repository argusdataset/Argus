"""The 8-K signal's decision, and the tolerance around FMP's field names.

Two things are being checked here that nothing else can check. First,
that `raised` is a plain `bool` and stays one — the whole reason this
signal is shaped differently from the volume anomaly beside it. Second,
that a payload whose keys are not the ones anyone guessed still produces
a usable row, because FMP's Ultimate plan was not purchased when this was
written and every field name in it is documented rather than verified.

The aggregate query behind these readings is tested against a real
PostgreSQL in `tests/integration/news_signals/`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from core.news_signals.filings import (
    FIELD_ALIASES,
    FiledFilings,
    FilingTranslationError,
    SecFilingSignal,
    evaluate_filing,
    extract_item_numbers,
    resolve_field,
    translate_filing,
)
from data.provider_adapters.fmp.models import FetchProvenance, SecFiling

SCAN_DATE = date(2026, 3, 10)
AS_OF = datetime(2026, 3, 10, 21, 0, tzinfo=UTC)
FETCHED_AT = datetime(2026, 3, 10, 22, 0, tzinfo=UTC)


def _filing(**payload) -> SecFiling:
    return SecFiling(
        provenance=FetchProvenance(
            endpoint="sec_8k_latest", url_path="/stable/8k-latest", fetched_at=FETCHED_AT
        ),
        symbol="TEST",
        form_type="8-K",
        raw=payload,
    )


def _evaluate(filed: FiledFilings | None) -> SecFilingSignal:
    return evaluate_filing(
        security_id=uuid4(),
        scan_date=SCAN_DATE,
        as_of=AS_OF,
        filed=filed,
        config_version_label="test-version",
    )


# --------------------------------------------------------------------------
# The decision: binary, never undetermined
# --------------------------------------------------------------------------


def test_a_filing_on_the_day_raises():
    signal = _evaluate(FiledFilings(count=1, item_numbers=("5.02",)))

    assert signal.raised is True
    assert signal.item_numbers == ("5.02",)


def test_no_filing_on_the_day_is_a_measured_false_not_an_absence():
    """The distinction this signal is built on. A security that filed
    nothing did not file — there is no baseline to be short of, so `None`
    would be claiming an uncertainty that does not exist here."""
    signal = _evaluate(None)

    assert signal.raised is False
    assert isinstance(signal.raised, bool)
    assert signal.item_numbers == ()


def test_the_verdict_is_never_none_for_any_input():
    """Guards the difference from `NewsVolumeSignal.raised`, which is
    deliberately tri-state. A future edit unifying the two would break
    this rather than quietly making 8-K absence look undetermined."""
    for filed in (None, FiledFilings(count=0, item_numbers=()), FiledFilings(3, ("1.01",))):
        assert _evaluate(filed).raised is not None


def test_a_filing_with_no_readable_item_numbers_still_raises():
    """*That* a filing happened is the signal; *which* item it disclosed
    is the colour. An unparseable item list must not suppress a real
    material event."""
    signal = _evaluate(FiledFilings(count=1, item_numbers=()))

    assert signal.raised is True
    assert signal.item_numbers == ()
    assert signal.detail["items_resolved"] is False


def test_several_filings_in_one_day_raise_once_and_keep_every_item():
    signal = _evaluate(FiledFilings(count=3, item_numbers=("5.02", "1.01", "2.02")))

    assert signal.raised is True
    assert signal.detail["filing_count"] == 3
    assert set(signal.item_numbers) == {"5.02", "1.01", "2.02"}


def test_the_stored_shape_carries_the_configuration_that_produced_it():
    payload = _evaluate(FiledFilings(count=1, item_numbers=("5.02",))).as_dict()

    assert payload["config_version_label"] == "test-version"
    assert payload["raised"] is True
    assert payload["scan_date"] == SCAN_DATE.isoformat()


# --------------------------------------------------------------------------
# Item-number extraction
# --------------------------------------------------------------------------


def test_items_are_read_from_a_dedicated_field():
    items, key = extract_item_numbers({"items": "5.02,1.01"})

    assert items == ("5.02", "1.01")
    assert key == "items"


def test_items_are_read_out_of_prose_when_there_is_no_dedicated_field():
    """Some feeds only carry the item inside a description or headline."""
    items, key = extract_item_numbers(
        {"description": "Item 5.02 Departure of Directors and Item 1.01 Material Agreement"}
    )

    assert items == ("5.02", "1.01")
    assert key == "description"


def test_item_order_is_preserved_and_duplicates_dropped():
    items, _ = extract_item_numbers({"items": "5.02, 1.01, 5.02"})

    assert items == ("5.02", "1.01")


def test_a_payload_mentioning_no_item_numbers_yields_an_empty_tuple():
    items, key = extract_item_numbers({"description": "Current report"})

    assert items == ()
    assert key == "description"


def test_a_payload_with_no_recognisable_field_at_all_says_so():
    items, key = extract_item_numbers({"somethingElse": "5.02"})

    assert items == ()
    assert key is None


def test_a_year_like_number_is_not_mistaken_for_an_item():
    """`2026.03` is not an item number and `10.5` is not either — the
    pattern requires exactly two digits after the dot."""
    items, _ = extract_item_numbers({"items": "filed 2026.031 covering 10.5 percent"})

    assert items == ()


# --------------------------------------------------------------------------
# Field-name tolerance
# --------------------------------------------------------------------------


@pytest.mark.parametrize("alias", FIELD_ALIASES["filed_at"])
def test_every_documented_spelling_of_the_filing_date_resolves(alias: str):
    """The point of the alias table: whichever of these FMP turns out to
    use, ingestion works and the row records which one it was."""
    stored = translate_filing(_filing(**{alias: "2026-03-10 16:31:00"}), uuid4())

    assert stored.pit.event_time.date() == SCAN_DATE
    assert stored.lineage["resolved_fields"]["filed_at"] == alias


@pytest.mark.parametrize("alias", FIELD_ALIASES["link"])
def test_every_documented_spelling_of_the_link_resolves(alias: str):
    stored = translate_filing(
        _filing(acceptedDate="2026-03-10 16:31:00", **{alias: "https://sec.gov/f.htm"}), uuid4()
    )

    assert stored.link == "https://sec.gov/f.htm"
    assert stored.lineage["resolved_fields"]["link"] == alias


def test_the_first_matching_alias_wins():
    """Ordered, not arbitrary: `acceptedDate` is the more precise of the
    two and comes first in the table."""
    stored = translate_filing(
        _filing(acceptedDate="2026-03-10 16:31:00", filingDate="2026-03-09"), uuid4()
    )

    assert stored.lineage["resolved_fields"]["filed_at"] == "acceptedDate"
    assert stored.pit.event_time.date() == SCAN_DATE


def test_a_filing_with_no_resolvable_date_is_rejected_rather_than_dated():
    """Every plausible default here is a PIT lie — the same rule
    `translate_fundamental` applies when a statement has no accepted date."""
    with pytest.raises(FilingTranslationError, match="no resolvable filing timestamp"):
        translate_filing(_filing(finalLink="https://sec.gov/f.htm"), uuid4())


def test_an_unknown_payload_keeps_every_field_it_could_not_read():
    """Nothing is dropped: a field this adapter did not recognise today is
    what a correction reads tomorrow."""
    stored = translate_filing(
        _filing(acceptedDate="2026-03-10 16:31:00", cik="0000320193", somethingNew=42), uuid4()
    )

    assert stored.data["cik"] == "0000320193"
    assert stored.data["somethingNew"] == 42


def test_resolve_field_reports_the_absence_rather_than_guessing():
    assert resolve_field({}, "link") == (None, None)
    assert resolve_field({"link": ""}, "link") == (None, None)


# --------------------------------------------------------------------------
# PIT timestamps
# --------------------------------------------------------------------------


def test_a_filing_is_knowable_when_accepted_plus_the_configured_lag():
    """An SEC filing is public the moment it is accepted, so event and
    observation are the same instant — unlike a fundamental, whose period
    ended long before anyone could read the numbers."""
    stored = translate_filing(_filing(acceptedDate="2026-03-10T16:31:00Z"), uuid4())

    assert stored.pit.event_time == stored.pit.observation_time
    assert stored.pit.availability_time > stored.pit.observation_time
    assert stored.pit.ingestion_time == FETCHED_AT


def test_a_date_without_a_time_is_anchored_to_end_of_day():
    """Midnight would make the filing readable a full day before it can be
    evidenced — the same choice `translate_fundamental` makes for
    `filing_date`."""
    stored = translate_filing(_filing(filingDate="2026-03-10"), uuid4())

    assert stored.pit.event_time.date() == SCAN_DATE
    assert stored.pit.event_time.hour == 23
