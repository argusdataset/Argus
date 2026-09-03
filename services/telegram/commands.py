"""Reading a Telegram update. Parsing only — no database, no decisions.

Telegram sends one JSON object per event, and the events this bot cares
about are two: a private message saying `/start` and one saying `/stop`.
Everything else Telegram can send — edited messages, channel posts,
callback queries, a photo, a sticker, someone typing "hello" — is
**ignored, not rejected**. That distinction matters at the protocol
level: Telegram retries a webhook that answers with an error, so
returning 4xx for a sticker would turn one sticker into a retry loop.

## Command syntax is looser than it looks

In a group chat Telegram appends the bot's username, so `/start` arrives
as `/start@ArgusAlertsBot`. Clients also send whatever case the user
typed, and a user who types `/Start ` with a trailing space means
`/start`. All three are normalised here rather than in the handler, so
the handler branches on an enum instead of on string shapes.

The bot only sends messages to chats that asked for them, so a group
that adds the bot and types `/start` gets group alerts. That is a
supported outcome rather than an accident: `chat_id` is negative for a
group, which the schema's `BIGINT` accommodates.

## Nothing here trusts the payload

The endpoint is open to the internet — Telegram publishes no source
addresses to allowlist — so every field is read defensively: missing
keys, wrong types, a `chat.id` that is a string, a body that is not an
object at all. The authenticity check is a separate concern and lives in
`app.py`; this file's job is to not crash on rubbish.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = ["Command", "ParsedUpdate", "parse_update"]


class Command(StrEnum):
    """The two commands this bot answers, and everything else."""

    START = "start"
    STOP = "stop"
    #: A well-formed message that is not a command this bot knows.
    UNKNOWN = "unknown"


#: The message containers a `/start` can plausibly arrive in. `message`
#: is the ordinary case; `channel_post` is a channel the bot was added
#: to. `edited_message` is deliberately absent — editing an old message
#: into `/stop` should not unsubscribe anybody, because the edit carries
#: no evidence about when, or whether, the user meant it now.
_MESSAGE_KEYS = ("message", "channel_post")


@dataclass(frozen=True, slots=True)
class ParsedUpdate:
    """What one webhook body turned out to be."""

    chat_id: int | None
    command: Command

    @property
    def actionable(self) -> bool:
        """Whether this update asks for a subscription change."""
        return self.chat_id is not None and self.command in (Command.START, Command.STOP)


def parse_update(body: Any, *, command_limit: int) -> ParsedUpdate:
    """Read a Telegram update into a chat id and a command.

    Returns `Command.UNKNOWN` for anything unrecognised, and never
    raises: a parser on a public endpoint that raises on malformed input
    has handed the internet a way to fill the error log.
    """
    if not isinstance(body, dict):
        return ParsedUpdate(chat_id=None, command=Command.UNKNOWN)

    message = _first_message(body)
    if message is None:
        return ParsedUpdate(chat_id=None, command=Command.UNKNOWN)

    return ParsedUpdate(
        chat_id=_chat_id(message),
        command=_command(message.get("text"), limit=command_limit),
    )


def _first_message(body: dict[str, Any]) -> dict[str, Any] | None:
    for key in _MESSAGE_KEYS:
        candidate = body.get(key)
        if isinstance(candidate, dict):
            return candidate
    return None


def _chat_id(message: dict[str, Any]) -> int | None:
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    identifier = chat.get("id")
    # `bool` is an `int` in Python and `True` would become chat 1.
    if isinstance(identifier, bool) or not isinstance(identifier, int):
        return None
    return identifier


def _command(text: Any, *, limit: int) -> Command:
    if not isinstance(text, str):
        return Command.UNKNOWN

    head = text.strip()[:limit]
    if not head.startswith("/"):
        return Command.UNKNOWN

    # `/start@ArgusAlertsBot extra words` -> `start`
    word = head.split()[0]
    name = word[1:].split("@", 1)[0].lower()

    if name == Command.START.value:
        return Command.START
    if name == Command.STOP.value:
        return Command.STOP
    return Command.UNKNOWN
