"""How the client classifies a failed send, and what it refuses to log.

Every case here is driven through a real `httpx.MockTransport`, so the
code under test is the code that runs in production — URL construction,
JSON body, status handling — rather than a patched method.
"""

from __future__ import annotations

import httpx
import pytest

from packages.config.secrets import SecretNotFoundError, SecretsProvider
from services.telegram.client import BOT_TOKEN_SECRET, SendOutcome, TelegramClient

TOKEN = "123456:test-token-do-not-use"  # noqa: S105 - a fixture, not a credential


class StubSecrets(SecretsProvider):
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = {BOT_TOKEN_SECRET: TOKEN} if values is None else values

    def get_secret(self, key: str) -> str:
        try:
            return self._values[key]
        except KeyError:
            raise SecretNotFoundError(key) from None


def _client(handler, **kwargs) -> TelegramClient:
    waits: list[float] = []
    client = TelegramClient(
        secrets=StubSecrets(),
        transport=httpx.MockTransport(handler),
        sleep=waits.append,
        **kwargs,
    )
    client.waits = waits  # type: ignore[attr-defined]
    return client


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {}})


def _error(status: int, description: str, **extra) -> httpx.Response:
    body = {"ok": False, "error_code": status, "description": description, **extra}
    return httpx.Response(status, json=body)


def test_a_successful_send_is_delivered():
    with _client(_ok) as client:
        result = client.send_message(42, "hello")

    assert result.delivered
    assert result.outcome is SendOutcome.DELIVERED


def test_the_token_is_in_the_path_and_the_body_carries_the_chat():
    """Pinned because the URL shape is where the credential leak risk lives.

    Asserting it here means the one place the token appears is a place
    this test knows about, which is what makes the "never log a URL" rule
    checkable rather than aspirational.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(request)

    with _client(handler) as client:
        client.send_message(42, "hello")

    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
    assert b'"chat_id":42' in seen[0].content.replace(b" ", b"")


def test_a_blocked_user_is_unreachable_rather_than_a_failure():
    """403 means the user blocked the bot. A normal outcome, not an incident."""
    with _client(lambda r: _error(403, "Forbidden: bot was blocked by the user")) as client:
        result = client.send_message(42, "hello")

    assert result.outcome is SendOutcome.UNREACHABLE


@pytest.mark.parametrize(
    "description",
    [
        "Bad Request: chat not found",
        "Forbidden: user is deactivated",
        "Forbidden: bot was kicked from the group chat",
    ],
)
def test_a_gone_chat_is_unreachable(description: str):
    """A 400 that means "this chat will never accept a message" is permanent.

    Distinguished from other 400s by the description, because Telegram
    reuses the status code for genuinely retryable request problems.
    """
    with _client(lambda r: _error(400, description)) as client:
        assert client.send_message(42, "x").outcome is SendOutcome.UNREACHABLE


def test_an_ordinary_bad_request_is_transient_not_a_deactivation():
    """Deactivating a subscriber on a 400 we do not understand would be a bug.

    It costs somebody their subscription for a mistake in our own
    request, which they would never know had happened.
    """
    with _client(lambda r: _error(400, "Bad Request: message text is empty")) as client:
        assert client.send_message(42, "x").outcome is SendOutcome.TRANSIENT


def test_a_server_error_is_transient():
    with _client(lambda r: httpx.Response(502, text="bad gateway")) as client:
        assert client.send_message(42, "x").outcome is SendOutcome.TRANSIENT


def test_a_transport_failure_does_not_raise_and_does_not_carry_the_url():
    """httpx's own exception message contains the request URL, which contains
    the token. Only the exception type is kept."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed", request=request)

    with _client(handler) as client:
        result = client.send_message(42, "x")

    assert result.outcome is SendOutcome.TRANSIENT
    assert TOKEN not in result.detail
    assert result.detail == "transport:ConnectError"


def test_a_rate_limit_obeys_telegrams_own_retry_after_then_succeeds():
    """The server's number beats any local guess, and one retry is enough."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return _error(429, "Too Many Requests", parameters={"retry_after": 7})
        return _ok(request)

    client = _client(handler)
    with client:
        result = client.send_message(42, "x")

    assert result.delivered
    assert 7.0 in client.waits  # type: ignore[attr-defined]


def test_a_second_rate_limit_gives_up_rather_than_stalling_the_run():
    """Waiting again after obeying the server's own figure would not fix it.

    Dropping the message keeps the run moving for every subscriber behind
    this one; nothing downstream depends on any single alert arriving.
    """
    with _client(lambda r: _error(429, "Too Many Requests", parameters={"retry_after": 1})) as c:
        assert c.send_message(42, "x").outcome is SendOutcome.TRANSIENT


def test_the_client_paces_itself_before_every_send():
    """Telegram throttles around 30/second; exceeding it makes the run slower."""
    client = _client(_ok, seconds_between_sends=0.05)
    with client:
        client.send_message(1, "a")
        client.send_message(2, "b")

    assert client.waits[:2] == [0.05, 0.05]  # type: ignore[attr-defined]


def test_the_repr_does_not_print_the_token():
    """The default dataclass-ish repr would, and a repr ends up in tracebacks."""
    with _client(_ok) as client:
        assert TOKEN not in repr(client)
        assert repr(client) == "TelegramClient(token=***)"


def test_a_missing_token_raises_the_secrets_error_rather_than_starting():
    with pytest.raises(SecretNotFoundError):
        TelegramClient(secrets=StubSecrets({}))
