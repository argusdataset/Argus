"""Low-level FMP HTTP client.

Owns the concerns every request shares: authentication, rate limiting,
retry with backoff, caching, and turning a response into either a parsed
body or a specific, distinguishable error.

The API key is read once through `SecretsProvider` and never stored on a
config object, never logged, and never included in a cache key or an
error message. `_redact` is applied to anything derived from a URL before
it can reach an exception or a log line.
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from data.provider_adapters.fmp.cache import CacheEntry, ResponseCache, cache_key
from data.provider_adapters.fmp.endpoints import Endpoint, ResponseFormat
from data.provider_adapters.fmp.errors import (
    FmpAuthenticationError,
    FmpProviderError,
    FmpRateLimitError,
    FmpTransportError,
)
from data.provider_adapters.fmp.models import FetchProvenance
from data.provider_adapters.fmp.rate_limit import RateLimiter, backoff_delay
from packages.config import AppConfig, SecretsProvider, get_config, get_secrets_provider

#: Key the FMP credential is stored under in the secrets backend.
FMP_API_KEY_SECRET = "FMP_API_KEY"

#: How long a mutable response (listings, calendars, news) stays cached.
DEFAULT_MUTABLE_MAX_AGE = timedelta(hours=12)

_API_KEY_PARAM = "apikey"


def _redact(text: str, secret: str) -> str:
    """Remove the API key from anything that might be surfaced."""
    return text.replace(secret, "***") if secret else text


class FmpClient:
    """Async HTTP client for FMP.

    Use as an async context manager so the underlying connection pool is
    closed deterministically:

        async with FmpClient() as client:
            body = await client.get(endpoints.STOCK_LIST)
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        secrets: SecretsProvider | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self._config = config or get_config()
        provider_settings = self._config.providers
        secret_provider = secrets or get_secrets_provider(self._config)

        # Resolved once, held only in memory, never written anywhere.
        self._api_key = secret_provider.get_secret(FMP_API_KEY_SECRET)

        self._base_url = provider_settings.fmp_base_url.rstrip("/")
        self._max_retries = provider_settings.fmp_max_retries
        self._backoff_base = provider_settings.fmp_backoff_base_seconds
        self._backoff_max = provider_settings.fmp_backoff_max_seconds
        self._sleep = sleep

        self._limiter = RateLimiter(
            provider_settings.fmp_requests_per_minute,
            provider_settings.fmp_bulk_requests_per_minute,
        )
        self._concurrency = asyncio.Semaphore(provider_settings.fmp_max_concurrency)
        self._cache = ResponseCache(
            provider_settings.fmp_cache_dir,
            enabled=provider_settings.fmp_cache_enabled,
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=provider_settings.fmp_request_timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> FmpClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def cache(self) -> ResponseCache:
        return self._cache

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    async def get(
        self,
        endpoint: Endpoint,
        *,
        params: dict[str, Any] | None = None,
        path_params: dict[str, str] | None = None,
        force_refresh: bool = False,
    ) -> tuple[Any, FetchProvenance]:
        """Fetch one endpoint, returning its parsed body and provenance.

        Raises a specific FmpError subclass on failure — never returns
        empty data to signal a problem.
        """
        request_params = dict(params or {})
        url_path = endpoint.render(**(path_params or {}))
        key = cache_key(endpoint.name, url_path, request_params)
        max_age = None if endpoint.immutable else DEFAULT_MUTABLE_MAX_AGE

        if not force_refresh:
            cached = self._cache.get(key, max_age=max_age)
            if cached is not None:
                return cached.body, FetchProvenance(
                    endpoint=endpoint.name,
                    url_path=url_path,
                    # The ORIGINAL fetch time, not now. See cache.py.
                    fetched_at=cached.fetched_at,
                    request_params=request_params,
                    from_cache=True,
                )

        body, fetched_at = await self._request_with_retries(endpoint, url_path, request_params)
        self._cache.set(key, CacheEntry(body=body, fetched_at=fetched_at, status_code=200))
        return body, FetchProvenance(
            endpoint=endpoint.name,
            url_path=url_path,
            fetched_at=fetched_at,
            request_params=request_params,
            from_cache=False,
        )

    async def _request_with_retries(
        self,
        endpoint: Endpoint,
        url_path: str,
        request_params: dict[str, Any],
    ) -> tuple[Any, datetime]:
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            await self._limiter.acquire(endpoint.tier)
            try:
                async with self._concurrency:
                    response = await self._client.get(
                        url_path,
                        params={**request_params, _API_KEY_PARAM: self._api_key},
                    )
            except httpx.TimeoutException as exc:
                last_error = FmpTransportError(f"Timeout requesting {url_path}: {exc}")
            except httpx.TransportError as exc:
                last_error = FmpTransportError(
                    _redact(f"Transport failure requesting {url_path}: {exc}", self._api_key)
                )
            else:
                fetched_at = datetime.now(UTC)
                try:
                    return self._parse(endpoint, response, url_path), fetched_at
                except FmpRateLimitError as exc:
                    last_error = exc
                    if attempt < self._max_retries:
                        await self._sleep(
                            backoff_delay(
                                attempt,
                                base_seconds=self._backoff_base,
                                max_seconds=self._backoff_max,
                                retry_after_seconds=exc.retry_after_seconds,
                            )
                        )
                    continue
                except FmpProviderError as exc:
                    # 5xx is worth retrying; a malformed body from a 200 is
                    # not going to fix itself.
                    if exc.status_code is None or exc.status_code < 500:
                        raise
                    last_error = exc
                except FmpAuthenticationError:
                    # Retrying with the same rejected key is pointless.
                    raise

            if attempt < self._max_retries:
                await self._sleep(
                    backoff_delay(
                        attempt,
                        base_seconds=self._backoff_base,
                        max_seconds=self._backoff_max,
                    )
                )

        raise last_error or FmpProviderError(f"Request to {url_path} failed after retries.")

    def _parse(self, endpoint: Endpoint, response: httpx.Response, url_path: str) -> Any:
        """Turn a response into a body, or raise the right specific error."""
        status = response.status_code

        if status == 429:
            retry_after = response.headers.get("Retry-After")
            raise FmpRateLimitError(
                f"Rate limited on {url_path}.",
                retry_after_seconds=float(retry_after) if retry_after else None,
            )
        if status in (401, 403):
            raise FmpAuthenticationError(f"FMP rejected the API key on {url_path} (HTTP {status}).")
        if status >= 500:
            raise FmpProviderError(f"FMP server error on {url_path}.", status_code=status)
        if status >= 400:
            raise FmpProviderError(
                _redact(
                    f"FMP returned HTTP {status} on {url_path}: {response.text[:200]}",
                    self._api_key,
                ),
                status_code=status,
            )

        if endpoint.response_format is ResponseFormat.CSV:
            return list(csv.DictReader(io.StringIO(response.text)))

        try:
            body = response.json()
        except ValueError as exc:
            raise FmpProviderError(
                f"FMP returned an unparseable body on {url_path}: {exc}", status_code=status
            ) from exc

        # FMP signals some failures with HTTP 200 and an error payload.
        # Treating that as data would put silent holes in the record.
        if isinstance(body, dict):
            message = body.get("Error Message") or body.get("error")
            if message:
                text = _redact(str(message), self._api_key)
                if "limit" in text.lower() or "bandwidth" in text.lower():
                    raise FmpRateLimitError(f"FMP reported a limit on {url_path}: {text}")
                if "key" in text.lower() or "authoriz" in text.lower():
                    raise FmpAuthenticationError(f"FMP rejected the request on {url_path}: {text}")
                raise FmpProviderError(
                    f"FMP error payload on {url_path}: {text}", status_code=status
                )

        return body
