"""Resumption, provenance, and rate-limit pacing.

A full-universe backfill runs for hours and will be interrupted. These
tests cover what has to be true for that to be survivable.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from data.provider_adapters.fmp.checkpoint import JobCheckpoint
from data.provider_adapters.fmp.endpoints import EndpointTier
from data.provider_adapters.fmp.rate_limit import RateLimiter, TokenBucket, backoff_delay
from tests.unit.fmp.conftest import fixture_handler, load_fixture

BARS = load_fixture("historical_price_eod_full.json")


def _price_handler(fail_for: set[str] | None = None):
    """Serve bars per symbol, optionally failing for some of them."""
    failures = fail_for or set()

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params.get("symbol", "")
        if symbol in failures:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=BARS)

    return handler


# --------------------------------------------------------------------------
# Checkpoint store
# --------------------------------------------------------------------------


def test_checkpoint_skips_units_completed_on_an_earlier_run(tmp_path):
    checkpoint = JobCheckpoint(tmp_path, "job")
    checkpoint.record("AAPL", succeeded=True)
    checkpoint.record("MSFT", succeeded=True)

    resumed = JobCheckpoint(tmp_path, "job")
    resumed.load()

    assert resumed.pending(["AAPL", "MSFT", "GOOG"]) == ["GOOG"]


def test_failed_units_are_retried_on_resume(tmp_path):
    """A transient failure must not become a permanent hole in history."""
    checkpoint = JobCheckpoint(tmp_path, "job")
    checkpoint.record("AAPL", succeeded=True)
    checkpoint.record("MSFT", succeeded=False, error="FmpProviderError")

    resumed = JobCheckpoint(tmp_path, "job")
    resumed.load()

    assert resumed.pending(["AAPL", "MSFT"]) == ["MSFT"]
    assert [record.unit for record in resumed.failures()] == ["MSFT"]


def test_a_torn_final_line_does_not_lose_earlier_progress(tmp_path):
    """A process killed mid-write leaves at most one malformed line."""
    checkpoint = JobCheckpoint(tmp_path, "job")
    checkpoint.record("AAPL", succeeded=True)
    checkpoint.record("MSFT", succeeded=True)

    with checkpoint.path.open("a", encoding="utf-8") as handle:
        handle.write('{"unit": "GOOG", "succ')  # interrupted mid-write

    resumed = JobCheckpoint(tmp_path, "job")
    resumed.load()

    assert resumed.is_complete("AAPL")
    assert resumed.is_complete("MSFT")
    assert not resumed.is_complete("GOOG")


def test_missing_checkpoint_file_means_a_fresh_start(tmp_path):
    checkpoint = JobCheckpoint(tmp_path / "does-not-exist", "job")
    checkpoint.load()

    assert checkpoint.pending(["AAPL"]) == ["AAPL"]


def test_checkpoint_is_flushed_immediately(tmp_path):
    """Its entire value is surviving an abrupt exit."""
    checkpoint = JobCheckpoint(tmp_path, "job")
    checkpoint.record("AAPL", succeeded=True, bars=2)

    lines = checkpoint.path.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["unit"] == "AAPL"
    assert json.loads(lines[0])["detail"] == {"bars": 2}


# --------------------------------------------------------------------------
# Backfill
# --------------------------------------------------------------------------


async def test_backfill_fetches_every_symbol(make_fetcher):
    fetcher, client = make_fetcher(_price_handler())
    async with client:
        report = await fetcher.backfill_daily_history(["AAPL", "MSFT", "GOOG"], job_name="t1")

    assert report.requested == 3
    assert report.succeeded == 3
    assert report.failed == {}


async def test_backfill_resumes_instead_of_restarting(make_fetcher):
    """The interrupted-run case this whole mechanism exists for."""
    fetcher, client = make_fetcher(_price_handler())
    async with client:
        await fetcher.backfill_daily_history(["AAPL", "MSFT"], job_name="resume")

    fetcher2, client2 = make_fetcher(_price_handler())
    async with client2:
        report = await fetcher2.backfill_daily_history(["AAPL", "MSFT", "GOOG"], job_name="resume")

    assert report.already_complete == 2
    assert report.succeeded == 1
    # Only the new symbol was requested; the cache also holds the others.
    requested = {request.url.params.get("symbol") for request in client2.test_transport.requests}
    assert requested == {"GOOG"}


async def test_one_failing_symbol_does_not_abort_the_run(make_fetcher):
    fetcher, client = make_fetcher(_price_handler(fail_for={"BROKEN"}))
    async with client:
        report = await fetcher.backfill_daily_history(
            ["AAPL", "BROKEN", "MSFT"], job_name="partial"
        )

    assert report.succeeded == 2
    assert set(report.failed) == {"BROKEN"}


async def test_a_failed_symbol_is_retried_on_the_next_run(make_fetcher):
    fetcher, client = make_fetcher(_price_handler(fail_for={"FLAKY"}))
    async with client:
        await fetcher.backfill_daily_history(["AAPL", "FLAKY"], job_name="flaky")

    # Second run: the provider has recovered.
    fetcher2, client2 = make_fetcher(_price_handler())
    async with client2:
        report = await fetcher2.backfill_daily_history(["AAPL", "FLAKY"], job_name="flaky")

    assert report.already_complete == 1
    assert report.succeeded == 1
    assert report.failed == {}


async def test_backfill_streams_records_to_a_callback(make_fetcher):
    """So a caller can persist incrementally rather than hold the universe."""
    seen: list[str] = []

    fetcher, client = make_fetcher(_price_handler())
    async with client:
        await fetcher.backfill_daily_history(
            ["AAPL", "MSFT"],
            job_name="stream",
            on_records=lambda result: seen.append(result.records[0].symbol),
        )

    assert sorted(seen) == ["AAPL", "MSFT"]


async def test_backfill_accepts_an_async_callback(make_fetcher):
    seen: list[str] = []

    async def collect(result) -> None:
        await asyncio.sleep(0)
        seen.append(result.records[0].symbol)

    fetcher, client = make_fetcher(_price_handler())
    async with client:
        await fetcher.backfill_daily_history(["AAPL"], job_name="async_cb", on_records=collect)

    assert seen == ["AAPL"]


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


async def test_every_record_carries_provider_endpoint_and_fetch_time(make_fetcher):
    """Module 05 needs all three: ingestion_time and data lineage."""
    fetcher, client = make_fetcher(fixture_handler({"/historical-price-eod/full": BARS}))
    async with client:
        result = await fetcher.fetch_daily_history("AAPL")

    for record in result.records:
        assert record.provenance.provider == "fmp"
        assert record.provenance.endpoint == "historical_price_eod_full"
        assert record.provenance.url_path.endswith("/historical-price-eod/full")
        assert record.provenance.fetched_at is not None


async def test_provenance_records_the_request_parameters(make_fetcher):
    fetcher, client = make_fetcher(fixture_handler({"/historical-price-eod/full": BARS}))
    async with client:
        result = await fetcher.fetch_daily_history("AAPL")

    assert result.provenance.request_params["symbol"] == "AAPL"


async def test_provenance_never_contains_the_api_key(make_fetcher):
    from tests.unit.fmp.conftest import TEST_API_KEY

    fetcher, client = make_fetcher(fixture_handler({"/historical-price-eod/full": BARS}))
    async with client:
        result = await fetcher.fetch_daily_history("AAPL")

    assert TEST_API_KEY not in json.dumps(result.provenance.model_dump(mode="json"))


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


async def test_token_bucket_allows_a_burst_up_to_its_capacity():
    clock = {"now": 0.0}
    bucket = TokenBucket(60, time_source=lambda: clock["now"])

    for _ in range(60):
        await bucket.acquire()

    assert bucket.available_tokens < 1.0


async def test_token_bucket_refills_over_time():
    clock = {"now": 0.0}
    bucket = TokenBucket(60, time_source=lambda: clock["now"])
    for _ in range(60):
        await bucket.acquire()

    clock["now"] = 10.0  # 10s at 1 token/s
    await bucket.acquire()

    assert bucket.available_tokens == pytest.approx(9.0, abs=0.01)


async def test_bulk_and_standard_endpoints_draw_on_separate_budgets():
    """FMP throttles bulk downloads far harder than standard endpoints."""
    clock = {"now": 0.0}
    limiter = RateLimiter(600, 6, time_source=lambda: clock["now"])

    for _ in range(6):
        await limiter.acquire(EndpointTier.BULK)

    assert limiter.bucket(EndpointTier.BULK).available_tokens < 1.0
    # Draining the bulk budget must not touch the standard one.
    assert limiter.bucket(EndpointTier.STANDARD).available_tokens == pytest.approx(600.0)


def test_backoff_doubles_and_is_capped():
    delays = [backoff_delay(n, base_seconds=1.0, max_seconds=10.0) for n in range(1, 6)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0]


def test_server_retry_after_overrides_computed_backoff():
    """The server knows better than any local guess."""
    delay = backoff_delay(1, base_seconds=1.0, max_seconds=60.0, retry_after_seconds=30.0)
    assert delay == 30.0


def test_retry_after_is_still_capped():
    delay = backoff_delay(1, base_seconds=1.0, max_seconds=10.0, retry_after_seconds=3600.0)
    assert delay == 10.0
