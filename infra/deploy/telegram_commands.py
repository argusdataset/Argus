"""Register the bot's command menu with Telegram. Run by a person, once.

`setMyCommands` tells Telegram which commands to offer in a chat's menu
button and in autocomplete. It is idempotent — the call replaces the
whole list — and it is deliberately **not** run at app startup.

## Why not on startup

The same reason `setWebhook` is not, and the reason is a rollback: a
registration that re-runs on every deploy is one that a rollback silently
undoes, or worse, one that a half-deployed revision silently changes. A
menu that quietly disagrees with the code is exactly the kind of thing
nobody reports as a bug — the button is just there, and does nothing.

## Why an entrypoint rather than a curl in the README

The list lives in `services/telegram/menus.BOT_COMMANDS`, in code, next
to the parser that has to handle every entry. A README command means the
JSON is hand-copied, and hand-copied JSON drifts. A test asserts every
registered command is one the parser actually answers; this module is
what makes that assertion true of the live bot as well as of the source.

    python -m infra.deploy.telegram_commands

Exit `0` when Telegram accepted the list, `1` when it refused or could
not be reached, `2` when the bot token is not resolvable — the same three
codes every other ARGUS entrypoint uses, for the same reason.
"""

from __future__ import annotations

import sys

from infra.deploy.cli import refuse_arguments
from infra.deploy.config import DeploymentProfile, profile_for
from infra.observability.logging import configure_logging, get_logger
from packages.config import SecretNotFoundError, SecretsProvider
from services.telegram.client import SendResult, TelegramClient
from services.telegram.config import TelegramConfig
from services.telegram.menus import BOT_COMMANDS

__all__ = ["CommandsNotReady", "main", "register_commands"]

_log = get_logger("argus.deploy.telegram_commands")


class CommandsNotReady(RuntimeError):
    """The bot token is not resolvable, so there is nothing to register with."""


def register_commands(
    *,
    config: TelegramConfig | None = None,
    profile: DeploymentProfile | None = None,
    secrets: SecretsProvider | None = None,
) -> SendResult:
    """Send `BOT_COMMANDS` to Telegram. Returns the client's own result."""
    settings = config or TelegramConfig()
    (profile or profile_for()).validate()

    try:
        client = TelegramClient(
            secrets=secrets,
            timeout=settings.settings.timeout,
            seconds_between_sends=settings.settings.seconds_between_sends,
        )
    except SecretNotFoundError as missing:
        raise CommandsNotReady(
            "The Telegram bot token is not resolvable, so the command menu cannot "
            "be registered. Set TELEGRAM_BOT_TOKEN; it is read through "
            "SecretsProvider and is never a settings default."
        ) from missing

    with client:
        result = client.set_my_commands(BOT_COMMANDS)

    _log.info(
        "bot command menu registered" if result.delivered else "command registration refused",
        extra={
            "event": "telegram_commands_registered",
            "commands": [name for name, _ in BOT_COMMANDS],
            "outcome": result.outcome.value,
            "detail": result.detail,
        },
    )
    return result


def main(argv: list[str] | None = None) -> int:
    refuse_arguments("infra.deploy.telegram_commands", argv)
    configure_logging()

    try:
        result = register_commands(profile=profile_for())
    except CommandsNotReady as not_ready:
        _log.error(
            "command registration prerequisite missing",
            extra={"event": "telegram_commands_not_ready", "detail": str(not_ready)},
        )
        return 2

    return 0 if result.delivered else 1


if __name__ == "__main__":  # pragma: no cover - run by hand
    raise SystemExit(main(sys.argv[1:]))
