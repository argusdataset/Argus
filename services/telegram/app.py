"""The bot's webhook surface. One route, and most of it is about trust.

Telegram delivers every `/start` and `/stop` by POSTing to a URL this
service exposes. That URL is on the public internet and Telegram
publishes no source addresses to allowlist, so the only thing separating
a real update from a forged one is a shared secret — and without one, the
endpoint is a way for anyone who learns the URL to subscribe arbitrary
chat ids (making ARGUS's bot message strangers) or to unsubscribe real
ones.

## So the secret token is required, not optional

`setWebhook` accepts a `secret_token`, and Telegram then sends it back on
every delivery as `X-Telegram-Bot-Api-Secret-Token`. This service
resolves the expected value through `SecretsProvider` at construction and
**refuses to start without it in any environment that is reachable** —
staging and production both. A public endpoint that authenticates when
configured and accepts everything when not is one environment variable
away from being open, and the missing variable is invisible until
somebody finds the URL.

Development is the one exception, and it is not a weaker check: an unset
secret there produces a **random** one, so nothing authenticates rather
than everything doing so. That keeps the "every service boots with only a
connection string" property the rest of the deployment relies on, without
the failure mode being an open endpoint — a developer who wants to
exercise a real delivery sets the variable, and the warning says so.

Compared with `hmac.compare_digest`, so a wrong guess takes the same time
as a right one.

## This service holds no bot token

The reply to `/start` is sent by *answering the webhook* — Telegram reads
a JSON body of `{"method": "sendMessage", ...}` on the webhook response
and performs that call itself. So the public-facing service needs no
outbound credential at all: only the dispatch cron holds
`TELEGRAM_BOT_TOKEN`. A compromise of this service cannot send messages
as the bot, which is worth more than the one thing it costs — Telegram
does not report back whether that reply was delivered, so a failed
confirmation is silent. A confirmation is the cheapest message in the
system to lose.

## Everything unrecognised gets 200

A sticker, an edited message, a callback query, a malformed body from a
scanner probing the URL: all answered `{"ok": true}` and ignored.
Telegram *retries* a webhook that answers with an error, so returning
4xx for a sticker converts one sticker into a retry loop. The only
non-200 this service issues is the 401 for a bad or missing secret
token, which is not a Telegram delivery at all.
"""

from __future__ import annotations

import hmac
import secrets as randomness
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from infra.deploy.config import DeploymentProfile, profile_for
from infra.observability.logging import get_logger
from infra.security.config import SecurityConfig
from infra.security.middleware import harden
from packages.config import SecretsProvider, bootstrap_secrets_provider
from packages.config.environment import Environment
from services.telegram import subscribers
from services.telegram.commands import Command, parse_update
from services.telegram.config import TelegramConfig
from services.telegram.messages import START_TEXT, STOP_TEXT

__all__ = ["SECRET_TOKEN_HEADER", "WEBHOOK_PATH", "WEBHOOK_SECRET", "create_app"]

_log = get_logger("argus.telegram.app")

#: Resolved through `SecretsProvider`. Never a settings default.
WEBHOOK_SECRET = "TELEGRAM_WEBHOOK_SECRET"

#: Telegram's own header name for the value passed to `setWebhook`.
SECRET_TOKEN_HEADER = "X-Telegram-Bot-Api-Secret-Token"

#: Fixed rather than derived from the token. A path containing the bot
#: token is a common trick for making the URL unguessable, and it puts
#: the credential into every access log, proxy log and browser history
#: that ever sees the URL. The secret header does the same job without
#: writing the token down eight places.
WEBHOOK_PATH = "/telegram/webhook"


#: Environments whose endpoints are reachable by somebody other than the
#: person who started the process. Both refuse to boot without the
#: secret; development substitutes a random one instead.
_EXPOSED = (Environment.STAGING, Environment.PRODUCTION)

#: Bytes of entropy in the development stand-in. Enough that it is not
#: guessable; the point is that it is not *knowable*, so no delivery
#: authenticates until a real value is configured.
_DEV_SECRET_BYTES = 32


class WebhookSecretMissing(RuntimeError):
    """The service cannot start, and starting anyway would be worse.

    Named and raised rather than warned about: an open subscribe/
    unsubscribe endpoint is not a degraded mode of this service, it is a
    different service.
    """


def create_app(
    engine: Engine,
    config: TelegramConfig | None = None,
    security: SecurityConfig | None = None,
    secrets: SecretsProvider | None = None,
    profile: DeploymentProfile | None = None,
) -> FastAPI:
    settings = config or TelegramConfig()
    expected = _expected_secret(secrets, profile or profile_for())

    app = FastAPI(
        title="ARGUS Telegram Bot",
        version="1",
        summary="Subscribe and unsubscribe from BREAKOUT_READY alerts.",
        description=(
            "Receives Telegram's webhook deliveries. Handles /start and /stop and "
            "nothing else. Holds no ARGUS account, issues no credential, and reads "
            "no market data — a subscriber is a Telegram chat id."
        ),
    )
    app.state.engine = engine
    app.state.config = settings
    app.state.webhook_secret = expected

    @app.post(WEBHOOK_PATH, include_in_schema=False)
    async def webhook(
        request: Request,
        connection: ConnectionDep,
        secret_token: Annotated[str | None, Header(alias=SECRET_TOKEN_HEADER)] = None,
    ) -> JSONResponse:
        """One Telegram update. Answers 200 to everything it accepts."""
        if not _authentic(secret_token, request.app.state.webhook_secret):
            _log.warning(
                "webhook delivery rejected",
                extra={
                    "event": "telegram_webhook_unauthenticated",
                    "has_header": secret_token is not None,
                },
            )
            return JSONResponse(status_code=401, content={"ok": False})

        body = await _json(request)
        update = parse_update(body, command_limit=settings.settings.command_limit)

        if not update.actionable or update.chat_id is None:
            return _acknowledge()

        if update.command is Command.START:
            subscribers.subscribe(connection, update.chat_id)
            _log.info(
                "telegram subscriber added",
                extra={"event": "telegram_subscribed", "chat_id": update.chat_id},
            )
            return _reply(update.chat_id, START_TEXT)

        subscribers.unsubscribe(connection, update.chat_id)
        _log.info(
            "telegram subscriber removed",
            extra={"event": "telegram_unsubscribed", "chat_id": update.chat_id},
        )
        return _reply(update.chat_id, STOP_TEXT)

    return harden(app, security=security)


def get_connection(request: Request) -> Iterator[Connection]:
    """A transaction per delivery. Ordinary semantics — nothing here refuses."""
    engine: Engine = request.app.state.engine
    with engine.begin() as connection:
        yield connection


ConnectionDep = Annotated[Connection, Depends(get_connection)]


def _expected_secret(secrets: SecretsProvider | None, profile: DeploymentProfile) -> str:
    # `bootstrap_secrets_provider`, not `get_secrets_provider`: every
    # deployed service runs with `DATABASE_URL` and nothing else, and
    # `AppConfig` cannot be built from that — see the helper's docstring
    # and KNOWN_ISSUES G3.
    provider = secrets or bootstrap_secrets_provider()
    try:
        value = provider.get_secret(WEBHOOK_SECRET)
    except Exception:  # noqa: BLE001 - any resolution failure means "not configured"
        value = ""

    if value.strip():
        return value

    if profile.environment in _EXPOSED:
        raise WebhookSecretMissing(
            f"{WEBHOOK_SECRET} is not set. This service exposes a public endpoint "
            "that subscribes and unsubscribes Telegram chats, and Telegram "
            "publishes no source addresses to allowlist — the secret token is the "
            "only thing distinguishing a real delivery from a forged one. Set it, "
            "then register it with setWebhook's `secret_token` parameter (see "
            "services/telegram/README.md)."
        )

    # Worded to avoid the words `secret`, `token` and `credential`, and to
    # name no variable — which is not squeamishness.
    # `infra/security/scrubbing.py` scans every logging call site in the
    # project for credential-shaped arguments, and to a scanner a message
    # that merely *mentions* one is indistinguishable from one that emits
    # it. The scan is worth more than the phrasing, so the log points at
    # the README and the exception below does the naming.
    _log.warning(
        "webhook authentication is not configured; using a random value",
        extra={
            "event": "telegram_webhook_auth_unconfigured",
            "environment": profile.environment.value,
            "detail": (
                "A random value is in use, so no Telegram delivery will "
                "authenticate — which is the safe direction. See "
                "services/telegram/README.md for the variable to set."
            ),
        },
    )
    return randomness.token_urlsafe(_DEV_SECRET_BYTES)


def _authentic(supplied: str | None, expected: str) -> bool:
    """Constant-time comparison, so a wrong guess is not a faster no."""
    if supplied is None:
        return False
    return hmac.compare_digest(supplied, expected)


async def _json(request: Request) -> Any:
    """The body, or `None`. A malformed body is a thing to ignore, not a 400."""
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 - any parse failure means "not an update"
        return None


def _acknowledge() -> JSONResponse:
    return JSONResponse(status_code=200, content={"ok": True})


def _reply(chat_id: int, text: str) -> JSONResponse:
    """Answer the webhook with the reply, instead of calling the API.

    See the module docstring: this is what lets the public service hold
    no bot token.
    """
    return JSONResponse(
        status_code=200,
        content={"method": "sendMessage", "chat_id": chat_id, "text": text},
    )
