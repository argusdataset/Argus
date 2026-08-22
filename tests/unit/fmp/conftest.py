"""Fixtures for the FMP adapter tests.

Every test runs against recorded fixture payloads through an
`httpx.MockTransport`. No test makes a live API call — the suite must be
runnable without a key, without network, and without spending request
budget, and results must not change because a provider's data did.

Using MockTransport rather than patching the adapter's methods keeps the
real code path under test: URL construction, auth parameter injection,
rate limiting, caching, retry, and error classification all execute
exactly as they would in production.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from data.provider_adapters.fmp.client import FmpClient
from data.provider_adapters.fmp.fetchers import FmpFetcher
from packages.config.secrets import SecretNotFoundError, SecretsProvider
from packages.config.settings import AppConfig

FIXTURES = Path(__file__).parent / "fixtures"

TEST_API_KEY = "test-api-key-do-not-use"


def load_fixture(name: str) -> Any:
    path = FIXTURES / name
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")


class StubSecrets(SecretsProvider):
    """In-memory secrets, so no .env file is needed."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = {"FMP_API_KEY": TEST_API_KEY} if values is None else values

    def get_secret(self, key: str) -> str:
        try:
            return self._values[key]
        except KeyError:
            raise SecretNotFoundError(key) from None


@pytest.fixture
def config(tmp_path, monkeypatch) -> AppConfig:
    """Config pointed at a temp cache/checkpoint dir, with fast backoff."""
    for key, value in {
        "ARGUS_DATABASE__PORT": "5432",
        "ARGUS_DATABASE__NAME": "argus_test",
        "ARGUS_DATABASE__USER": "argus_test",
        "ARGUS_PROVIDERS__FMP_BASE_URL": "https://fmp.test",
        "ARGUS_PROVIDERS__FMP_CACHE_DIR": str(tmp_path / "cache"),
        "ARGUS_PROVIDERS__FMP_CHECKPOINT_DIR": str(tmp_path / "checkpoints"),
        # High enough that the limiter never gates a unit test.
        "ARGUS_PROVIDERS__FMP_REQUESTS_PER_MINUTE": "6000",
        "ARGUS_PROVIDERS__FMP_BULK_REQUESTS_PER_MINUTE": "600",
        "ARGUS_PROVIDERS__FMP_BACKOFF_BASE_SECONDS": "0.001",
        "ARGUS_PROVIDERS__FMP_BACKOFF_MAX_SECONDS": "0.01",
    }.items():
        monkeypatch.setenv(key, value)
    return AppConfig()


class RecordingTransport(httpx.MockTransport):
    """MockTransport that also records every request it served."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []

        def recording_handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(recording_handler)


@pytest.fixture
def make_client(config):
    """Build an FmpClient wired to a caller-supplied response handler."""
    created: list[FmpClient] = []

    def factory(handler, *, secrets: SecretsProvider | None = None) -> FmpClient:
        transport = RecordingTransport(handler)
        client = FmpClient(
            config,
            secrets or StubSecrets(),
            transport=transport,
            # Never actually sleep during a retry test.
            sleep=_no_sleep,
        )
        client.test_transport = transport  # type: ignore[attr-defined]
        created.append(client)
        return client

    yield factory


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def make_fetcher(make_client, config):
    def factory(handler, *, secrets: SecretsProvider | None = None):
        client = make_client(handler, secrets=secrets)
        return FmpFetcher(client, config), client

    return factory


def json_response(payload: Any, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def fixture_handler(mapping: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Serve fixtures by URL-path suffix.

    Any unmapped path returns 404, so a test that hits an unexpected
    endpoint fails loudly instead of silently receiving an empty list.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, payload in mapping.items():
            if request.url.path.endswith(suffix):
                if isinstance(payload, httpx.Response):
                    return payload
                if isinstance(payload, str):
                    return httpx.Response(200, text=payload)
                return json_response(payload)
        return httpx.Response(404, json={"Error Message": f"No fixture for {request.url.path}"})

    return handler
