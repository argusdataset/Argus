"""What the bot does with everything Telegram can send it.

The endpoint is open to the internet and Telegram publishes no source
addresses to allowlist, so the parser sees whatever anybody sends it. The
two properties under test are that it recognises the two commands in
every shape a real client produces, and that it never raises on anything
else — a parser on a public endpoint that raises has handed the internet
a way to fill the error log.
"""

from __future__ import annotations

import pytest

from services.telegram.commands import Command, parse_update
from services.telegram.config import TelegramSettings

LIMIT = TelegramSettings().command_limit


def _parse(body):
    return parse_update(body, command_limit=LIMIT)


def _message(text, chat_id: int = 12345, key: str = "message"):
    return {"update_id": 1, key: {"message_id": 2, "chat": {"id": chat_id}, "text": text}}


@pytest.mark.parametrize(
    "text",
    ["/start", "/start@ArgusAlertsBot", "  /start  ", "/START", "/Start@Bot", "/start now please"],
)
def test_start_is_recognised_in_every_shape_a_client_sends(text: str):
    """Telegram appends the bot's username in groups; users type what they type.

    Normalised in the parser rather than the handler, so the handler
    branches on an enum instead of on string shapes.
    """
    parsed = _parse(_message(text))

    assert parsed.command is Command.START
    assert parsed.chat_id == 12345
    assert parsed.actionable


@pytest.mark.parametrize("text", ["/stop", "/stop@ArgusAlertsBot", "/STOP"])
def test_stop_is_recognised_in_every_shape(text: str):
    assert _parse(_message(text)).command is Command.STOP


@pytest.mark.parametrize(
    "text",
    ["hello", "", "start", "//start", "/started", "/starting", "/help", "/stopped"],
)
def test_near_misses_are_not_commands(text: str):
    """`/started` is not `/start`, and `start` without a slash is not either.

    Prefix matching here would mean a user typing `/stopwatch` gets
    unsubscribed, which is the kind of thing nobody reports as a bug —
    they just stop getting alerts.
    """
    parsed = _parse(_message(text))

    assert parsed.command is Command.UNKNOWN
    assert not parsed.actionable


def test_a_channel_post_counts_as_a_message():
    assert _parse(_message("/start", key="channel_post")).command is Command.START


def test_an_edited_message_is_not_acted_on():
    """Editing an old message into `/stop` must not unsubscribe anybody.

    An edit carries no evidence about when — or whether — the user meant
    it now, and a bot that acted on edits could be made to unsubscribe
    somebody by editing a message they sent last year.
    """
    parsed = _parse(_message("/stop", key="edited_message"))

    assert parsed.command is Command.UNKNOWN
    assert parsed.chat_id is None


def test_a_group_chats_negative_id_survives():
    """Group ids are negative, which is why the column is BIGINT and signed."""
    parsed = _parse(_message("/start", chat_id=-1001234567890))

    assert parsed.chat_id == -1001234567890
    assert parsed.actionable


@pytest.mark.parametrize(
    "body",
    [
        None,
        "not an object",
        42,
        [],
        {},
        {"message": None},
        {"message": "text"},
        {"message": {"chat": None, "text": "/start"}},
        {"message": {"chat": {}, "text": "/start"}},
        {"message": {"chat": {"id": "12345"}, "text": "/start"}},
        {"message": {"chat": {"id": 1}, "text": None}},
        {"message": {"chat": {"id": 1}}},
        {"callback_query": {"data": "/start"}},
    ],
)
def test_rubbish_is_ignored_rather_than_raising(body):
    """Every one of these is something a scanner or a bug could send."""
    parsed = _parse(body)

    assert not parsed.actionable


def test_a_boolean_chat_id_is_not_chat_one():
    """`bool` is an `int` in Python, so `True` would otherwise become chat 1."""
    parsed = _parse(_message("/start", chat_id=True))

    assert parsed.chat_id is None
    assert not parsed.actionable


def test_only_the_head_of_a_long_body_is_examined():
    """A command is a dozen characters; scanning an essay for one is free work.

    The bound also matters because this endpoint is open: a body with a
    megabyte of text before a `/stop` should cost a slice, not a scan.
    """
    padded = "x" * (LIMIT * 10) + " /start"

    assert _parse(_message(padded)).command is Command.UNKNOWN
