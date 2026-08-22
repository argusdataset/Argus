"""Client behaviour: auth, failure modes, retry, and caching.

These cover the parts of the adapter that exist because the real world is
hostile — rate limits, transient failures, interrupted runs — and that
would otherwise only be exercised in production.
"""

from __future__ import annotations

import httpx
import pytest

from data.provider_adapters.fmp import endpoints
from data.provider_adapters.fmp.client import FMP_API_KEY_SECRET
from data.provider_adapters.fmp.errors import (
    FmpAuthenticationError,
    FmpProviderError,
    FmpRateLimitError,
    FmpTransportError,
)
from packages.config.secrets import SecretNotFoundError
from tests.unit.fmp.conftest import (
    TEST_API_KEY,
    StubSecrets,
    fixture_handler,
    load_fixture,
)

# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


async def test_api_key_is_sent_as_a_query_parameter(make_client):
    client = make_client(fixture_handler({"/stock-list": []}))
    async with client:
        await client.get(endpoints.STOCK_LIST)

    request = client.test_transport.requests[0]
    assert request.url.params["apikey"] == TEST_API_KEY


async def test_api_key_comes_only_from_the_secrets_provider(make_client, config):
    """It is never a config field, so it cannot leak via a settings dump."""
    assert not any("api_key" in name for name in type(config.providers).model_fields)


async def test_missing_api_key_fails_immediately(make_client):
    with pytest.raises(SecretNotFoundError) as exc_info:
        make_client(fixture_handler({}), secrets=StubSecrets({}))

    assert FMP_API_KEY_SECRET in str(exc_info.value)


async def test_api_key_is_redacted_from_error_messages(make_client):
    """A 4xx body is echoed into the exception — the key must not ride along."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"Bad request for key {TEST_API_KEY}")

    client = make_client(handler)
    async with client:
        with pytest.raises(FmpProviderError) as exc_info:
            await client.get(endpoints.STOCK_LIST)

    assert TEST_API_KEY not in str(exc_info.value)
    assert "***" in str(exc_info.value)


# --------------------------------------------------------------------------
# Failure modes stay distinguishable
# --------------------------------------------------------------------------


async def test_rate_limit_raises_a_rate_limit_error(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "2"}, json={})

    client = make_client(handler)
    async with client:
        with pytest.raises(FmpRateLimitError) as exc_info:
            await client.get(endpoints.STOCK_LIST)

    assert exc_info.value.retry_after_seconds == 2.0


async def test_rejected_key_raises_authentication_error(make_client):
    client = make_client(lambda request: httpx.Response(401, json={}))
    async with client:
        with pytest.raises(FmpAuthenticationError):
            await client.get(endpoints.STOCK_LIST)


async def test_server_error_raises_provider_error(make_client):
    client = make_client(lambda request: httpx.Response(503, text="unavailable"))
    async with client:
        with pytest.raises(FmpProviderError) as exc_info:
            await client.get(endpoints.STOCK_LIST)

    assert exc_info.value.status_code == 503


async def test_timeout_raises_transport_error(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    client = make_client(handler)
    async with client:
        with pytest.raises(FmpTransportError):
            await client.get(endpoints.STOCK_LIST)


async def test_error_payload_returned_with_http_200_is_not_treated_as_data(make_client):
    """FMP signals some failures with 200 plus an error body."""
    client = make_client(fixture_handler({"/stock-list": load_fixture("error_message.json")}))
    async with client:
        with pytest.raises(FmpAuthenticationError):
            await client.get(endpoints.STOCK_LIST)


async def test_unparseable_body_raises_rather_than_returning_nothing(make_client):
    client = make_client(lambda request: httpx.Response(200, text="<html>nope</html>"))
    async with client:
        with pytest.raises(FmpProviderError):
            await client.get(endpoints.STOCK_LIST)


async def test_an_error_never_arrives_as_empty_data(make_client):
    """The whole point of the taxonomy: a failure must never look like 'no data'."""
    client = make_client(lambda request: httpx.Response(500, text="boom"))
    async with client:
        with pytest.raises(FmpProviderError):
            body, _ = await client.get(endpoints.STOCK_LIST)


# --------------------------------------------------------------------------
# Retry and backoff
# --------------------------------------------------------------------------


async def test_retries_after_a_rate_limit_then_succeeds(make_client):
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json=[{"symbol": "AAPL"}])

    client = make_client(handler)
    async with client:
        body, provenance = await client.get(endpoints.STOCK_LIST)

    assert attempts["count"] == 3
    assert body == [{"symbol": "AAPL"}]
    assert provenance.from_cache is False


async def test_retries_a_server_error_then_succeeds(make_client):
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json=[])

    client = make_client(handler)
    async with client:
        await client.get(endpoints.STOCK_LIST)

    assert attempts["count"] == 2


async def test_authentication_failure_is_not_retried(make_client):
    """Retrying a rejected key just burns request budget."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(403, json={})

    client = make_client(handler)
    async with client:
        with pytest.raises(FmpAuthenticationError):
            await client.get(endpoints.STOCK_LIST)

    assert attempts["count"] == 1


async def test_gives_up_after_the_configured_retry_budget(make_client, config):
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(429, json={})

    client = make_client(handler)
    async with client:
        with pytest.raises(FmpRateLimitError):
            await client.get(endpoints.STOCK_LIST)

    assert attempts["count"] == config.providers.fmp_max_retries


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------


async def test_second_identical_request_is_served_from_cache(make_client):
    client = make_client(fixture_handler({"/stock-list": load_fixture("stock_list.json")}))
    async with client:
        first_body, first_provenance = await client.get(endpoints.STOCK_LIST)
        second_body, second_provenance = await client.get(endpoints.STOCK_LIST)

    assert len(client.test_transport.requests) == 1
    assert first_body == second_body
    assert first_provenance.from_cache is False
    assert second_provenance.from_cache is True


async def test_cache_hit_preserves_the_original_fetch_timestamp(make_client):
    """Module 05 maps fetched_at to ingestion_time.

    Refreshing it on a cache read would claim the data was observed later
    than it really was, misstating the point-in-time record.
    """
    client = make_client(fixture_handler({"/stock-list": load_fixture("stock_list.json")}))
    async with client:
        _, original = await client.get(endpoints.STOCK_LIST)
        _, cached = await client.get(endpoints.STOCK_LIST)

    assert cached.fetched_at == original.fetched_at


async def test_different_parameters_are_cached_separately(make_client):
    client = make_client(
        fixture_handler(
            {"/historical-price-eod/full": load_fixture("historical_price_eod_full.json")}
        )
    )
    async with client:
        await client.get(endpoints.HISTORICAL_PRICE_EOD_FULL, params={"symbol": "AAPL"})
        await client.get(endpoints.HISTORICAL_PRICE_EOD_FULL, params={"symbol": "MSFT"})

    assert len(client.test_transport.requests) == 2


async def test_force_refresh_bypasses_the_cache(make_client):
    client = make_client(fixture_handler({"/stock-list": load_fixture("stock_list.json")}))
    async with client:
        await client.get(endpoints.STOCK_LIST)
        await client.get(endpoints.STOCK_LIST, force_refresh=True)

    assert len(client.test_transport.requests) == 2


async def test_api_key_is_not_part_of_the_cache_key(make_client, config):
    """Otherwise a key rotation would silently invalidate the whole cache."""
    client_one = make_client(fixture_handler({"/stock-list": load_fixture("stock_list.json")}))
    async with client_one:
        await client_one.get(endpoints.STOCK_LIST)

    client_two = make_client(
        fixture_handler({"/stock-list": load_fixture("stock_list.json")}),
        secrets=StubSecrets({FMP_API_KEY_SECRET: "a-completely-different-key"}),
    )
    async with client_two:
        _, provenance = await client_two.get(endpoints.STOCK_LIST)

    assert provenance.from_cache is True
    assert client_two.test_transport.requests == []
