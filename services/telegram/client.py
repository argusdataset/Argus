"""Talking to Telegram. The only file that holds the bot token.

## Synchronous, deliberately

Module 04's FMP adapter is async because it fans out ten thousand
requests and the concurrency is the point. This sends a few hundred
messages from a cron job with no deadline — nothing waits on it, and the
pacing below is a deliberate *slow down*, not a speed up. An async client
here would buy nothing and would put an event loop inside a process whose
other half is synchronous SQLAlchemy.

## The token is in the URL, so nothing here may log a URL

Telegram authenticates by putting the bot token in the path:
`https://api.telegram.org/bot<TOKEN>/sendMessage`. That makes the usual
habit — logging the request URL on failure — a credential leak into the
log aggregator. So this file logs the *method name* and never the URL,
and `__repr__` is overridden because the default would print the token
held on the instance. Module 04 reached the same conclusion from the
other direction and added `_redact`; here the answer is not to construct
the loggable string in the first place.

## Which failures mean what

Telegram answers a failed send with a JSON body carrying `error_code` and
`description`, and the codes are the useful part:

- **403** — the user blocked the bot, or deleted the chat. Permanent, and
  the correct response is to stop sending: the subscriber is deactivated.
  This is a normal outcome, not an incident.
- **400** with "chat not found" — an id that never existed or a chat that
  is gone. Also permanent, also deactivation.
- **429** — rate limited, with `parameters.retry_after` seconds. The
  server's own number is better information than any local guess, so it
  is obeyed once and the send retried.
- Anything else, including transport failures — transient as far as this
  module can tell. Logged, counted, and the run continues to the next
  subscriber.

The classification is returned rather than raised, because the caller's
response differs per class and an exception would flatten four outcomes
into one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from infra.observability.logging import get_logger
from packages.config import SecretsProvider, get_secrets_provider

__all__ = [
    "BOT_TOKEN_SECRET",
    "SendOutcome",
    "SendResult",
    "TelegramClient",
]

_log = get_logger("argus.telegram.client")

#: Resolved through `SecretsProvider`, never a settings field and never
#: committed. Same arrangement as `FMP_API_KEY`.
BOT_TOKEN_SECRET = "TELEGRAM_BOT_TOKEN"

_API_ROOT = "https://api.telegram.org"

#: Telegram's own codes. Named rather than inline so the branch below
#: reads as the taxonomy the module docstring describes.
_BLOCKED = 403
_BAD_REQUEST = 400
_RATE_LIMITED = 429

#: The `description` Telegram returns for an id that is not a reachable
#: chat. Matched case-insensitively on a substring because the wording has
#: varied ("chat not found", "Bad Request: chat not found").
_CHAT_GONE = ("chat not found", "user is deactivated", "bot was kicked")


class SendOutcome(StrEnum):
    DELIVERED = "delivered"
    #: The chat will never accept another message. Deactivate.
    UNREACHABLE = "unreachable"
    #: Might work next time. Logged and skipped.
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class SendResult:
    outcome: SendOutcome
    detail: str = ""

    @property
    def delivered(self) -> bool:
        return self.outcome is SendOutcome.DELIVERED


class TelegramClient:
    """Sends messages. Paces itself. Never logs a URL."""

    def __init__(
        self,
        *,
        secrets: SecretsProvider | None = None,
        timeout: float = 10.0,
        seconds_between_sends: float = 0.05,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        provider = secrets or get_secrets_provider()
        # Held in memory only. See the module docstring.
        self._token = provider.get_secret(BOT_TOKEN_SECRET)
        self._pace = seconds_between_sends
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=f"{_API_ROOT}/bot{self._token}",
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def __repr__(self) -> str:
        """Deliberately says nothing. The default would print the token."""
        return "TelegramClient(token=***)"

    def send_message(self, chat_id: int, text: str) -> SendResult:
        """Send one message, pacing first. Never raises."""
        self._sleep(self._pace)
        result = self._post("sendMessage", {"chat_id": chat_id, "text": text})
        if result.outcome is SendOutcome.TRANSIENT:
            _log.warning(
                "telegram send failed",
                extra={
                    "event": "telegram_send_failed",
                    "chat_id": chat_id,
                    "detail": result.detail,
                },
            )
        return result

    def _post(self, method: str, payload: dict[str, Any]) -> SendResult:
        try:
            response = self._client.post(f"/{method}", json=payload)
        except httpx.HTTPError as error:
            # The exception's own message can contain the request URL,
            # and the URL contains the token. Only the type is kept.
            return SendResult(SendOutcome.TRANSIENT, f"transport:{type(error).__name__}")

        if response.status_code == httpx.codes.OK:
            return SendResult(SendOutcome.DELIVERED)

        body = _body(response)
        description = str(body.get("description") or "")

        if response.status_code == _RATE_LIMITED:
            return self._retry_after(method, payload, body, description)

        if response.status_code == _BLOCKED or (
            response.status_code == _BAD_REQUEST and _is_gone(description)
        ):
            return SendResult(
                SendOutcome.UNREACHABLE, description or f"http:{response.status_code}"
            )

        return SendResult(SendOutcome.TRANSIENT, description or f"http:{response.status_code}")

    def _retry_after(
        self,
        method: str,
        payload: dict[str, Any],
        body: dict[str, Any],
        description: str,
    ) -> SendResult:
        """Wait the number of seconds Telegram asked for, then try once more.

        Once, not repeatedly: a second 429 after obeying the server's own
        figure means the run is over budget in a way another wait will
        not fix, and the message is dropped rather than the run stalling
        behind it. Nothing downstream depends on this message arriving.
        """
        parameters = body.get("parameters")
        seconds = 0.0
        if isinstance(parameters, dict):
            candidate = parameters.get("retry_after")
            if isinstance(candidate, int | float) and not isinstance(candidate, bool):
                seconds = float(candidate)

        _log.info(
            "telegram rate limited",
            extra={"event": "telegram_rate_limited", "retry_after": seconds},
        )
        self._sleep(seconds)

        try:
            response = self._client.post(f"/{method}", json=payload)
        except httpx.HTTPError as error:
            return SendResult(SendOutcome.TRANSIENT, f"transport:{type(error).__name__}")

        if response.status_code == httpx.codes.OK:
            return SendResult(SendOutcome.DELIVERED)
        return SendResult(SendOutcome.TRANSIENT, description or f"http:{response.status_code}")


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_gone(description: str) -> bool:
    lowered = description.lower()
    return any(marker in lowered for marker in _CHAT_GONE)
