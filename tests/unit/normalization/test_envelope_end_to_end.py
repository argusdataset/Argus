"""All three FMP envelope shapes reach an identical canonical record.

Runs the real Module 04 fetch path (via httpx.MockTransport, no live
calls) so envelope unwrapping, typed-record construction, and canonical
translation are all exercised together. Asserting the three shapes
produce byte-identical canonical output is the point: envelope
inconsistency must be fully absorbed at this boundary and never leak
downstream.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from data.normalization.translate import translate_daily_bar
from tests.unit.fmp.conftest import fixture_handler

SECURITY_ID = uuid4()

BAR_ROW = {
    "symbol": "AAPL",
    "date": "2024-01-03",
    "open": 184.22,
    "high": 185.88,
    "low": 183.43,
    "close": 184.25,
    "volume": 58414500,
    "adjClose": 183.63,
}

#: The three shapes Module 04 documented FMP returning.
ENVELOPES = {
    "bare_array": [BAR_ROW],
    "keyed_wrapper": {"symbol": "AAPL", "historical": [BAR_ROW]},
    "bare_object": BAR_ROW,
}


async def _canonical_from_envelope(make_fetcher, envelope):
    fetcher, client = make_fetcher(fixture_handler({"/historical-price-eod/full": envelope}))
    async with client:
        result = await fetcher.fetch_daily_history("AAPL")
    return [translate_daily_bar(bar, SECURITY_ID) for bar in result.records]


@pytest.mark.parametrize("shape", sorted(ENVELOPES))
async def test_each_envelope_shape_normalizes_to_canonical(make_fetcher, shape):
    canonical = await _canonical_from_envelope(make_fetcher, ENVELOPES[shape])

    assert len(canonical) == 1
    bar = canonical[0]
    assert bar.close_raw == Decimal("184.25")
    assert bar.volume_raw == 58414500
    assert bar.pit.event_time.date() == date(2024, 1, 3)


async def test_all_three_envelope_shapes_produce_identical_canonical_records(make_fetcher):
    """Envelope shape must be invisible past this boundary.

    `lineage.from_cache` is excluded from the comparison: the three
    fetches share a cache directory, so the second and third are served
    from it. That flag describes how the response was obtained, not what
    it contains, and it is the one field that legitimately differs.
    """
    results = {}
    for shape, envelope in ENVELOPES.items():
        canonical = await _canonical_from_envelope(make_fetcher, envelope)
        dumped = canonical[0].model_dump(mode="json")
        dumped["lineage"].pop("from_cache")
        results[shape] = dumped

    distinct = {repr(value) for value in results.values()}
    assert len(distinct) == 1, f"Envelope shape leaked into canonical output: {results}"


async def test_a_cached_fetch_keeps_the_original_ingestion_time(make_fetcher):
    """Module 04's cache guarantee, verified through to a canonical record.

    The second fetch is served from cache, and its canonical
    `ingestion_time` must still be the moment the data was really
    fetched. Using the cache-read time would claim ARGUS observed it
    later than it did.
    """
    first = await _canonical_from_envelope(make_fetcher, ENVELOPES["bare_array"])
    second = await _canonical_from_envelope(make_fetcher, ENVELOPES["bare_array"])

    assert second[0].lineage.from_cache is True
    assert second[0].pit.ingestion_time == first[0].pit.ingestion_time


async def test_canonical_record_carries_no_provider_field_names(make_fetcher):
    """Nothing downstream may see an FMP field name."""
    canonical = await _canonical_from_envelope(make_fetcher, ENVELOPES["bare_array"])
    dumped = canonical[0].model_dump(mode="json")

    for fmp_field in ("adjClose", "adjOpen", "unadjustedVolume", "changePercent"):
        assert fmp_field not in dumped

    assert set(dumped) == {
        "security_id",
        "pit",
        "lineage",
        "timeframe",
        "open_raw",
        "high_raw",
        "low_raw",
        "close_raw",
        "volume_raw",
        "open_adjusted",
        "high_adjusted",
        "low_adjusted",
        "close_adjusted",
        "volume_adjusted",
    }


async def test_provider_adjusted_values_are_not_used_as_canonical_adjusted(make_fetcher):
    """ARGUS recomputes adjustment so it stays reproducible and explainable.

    FMP's adjClose of 183.63 must not appear as the canonical adjusted
    close; translation leaves it unset for the adjustment step to fill.
    """
    canonical = await _canonical_from_envelope(make_fetcher, ENVELOPES["bare_array"])

    assert canonical[0].close_adjusted is None
    assert canonical[0].close_raw == Decimal("184.25")
