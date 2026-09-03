"""The menu presentation layer: keyboards, button labels, and command coverage.

Presentation is where a bug is least likely to raise and most likely to
be user-visible — a button whose label no longer parses is a button that
silently does nothing. So the tests here are mostly about the two lists
agreeing: what the keyboard offers, what `setMyCommands` registers, and
what the parser actually answers.
"""

from __future__ import annotations

import pytest

from services.telegram.commands import Command, parse_update
from services.telegram.config import TelegramSettings
from services.telegram.menus import (
    ALERTS_BUTTON,
    BOT_COMMANDS,
    BUTTON_COMMANDS,
    STATS_BUTTON,
    SUBSCRIBE_BUTTON,
    UNSUBSCRIBE_BUTTON,
    alerts_keyboard,
    main_keyboard,
)

LIMIT = TelegramSettings().command_limit


def _labels(keyboard: dict) -> list[str]:
    return [button["text"] for row in keyboard["keyboard"] for button in row]


def test_the_main_menu_offers_exactly_the_two_menus():
    assert _labels(main_keyboard()) == [ALERTS_BUTTON, STATS_BUTTON]


def test_the_keyboard_resizes_and_persists():
    """A keyboard that eats half a phone screen, or vanishes after one use,
    is a prompt rather than a menu."""
    keyboard = main_keyboard()

    assert keyboard["resize_keyboard"] is True
    assert keyboard["is_persistent"] is True


@pytest.mark.parametrize(
    ("subscribed", "offered", "hidden"),
    [
        (True, UNSUBSCRIBE_BUTTON, SUBSCRIBE_BUTTON),
        (False, SUBSCRIBE_BUTTON, UNSUBSCRIBE_BUTTON),
    ],
)
def test_the_alerts_menu_offers_only_the_action_that_would_do_something(
    subscribed: bool, offered: str, hidden: str
):
    """Showing both would mean one of them is always a no-op.

    A button that does nothing teaches a reader that the buttons are
    decorative, which is worse than one fewer button.
    """
    labels = _labels(alerts_keyboard(subscribed=subscribed))

    assert offered in labels
    assert hidden not in labels


@pytest.mark.parametrize("label", sorted(BUTTON_COMMANDS))
def test_every_button_label_parses_back_to_its_command(label: str):
    """The property that makes a reply keyboard work at all.

    A reply-keyboard button sends its own label as an ordinary message,
    so a label the parser does not recognise is a button that silently
    does nothing — and nothing else in the system would notice.
    """
    parsed = parse_update({"message": {"chat": {"id": 1}, "text": label}}, command_limit=LIMIT)

    assert parsed.command is BUTTON_COMMANDS[label]
    assert parsed.actionable


def test_a_sentence_containing_a_button_label_is_not_a_button_press():
    """Matched on the exact label, not as a substring."""
    for text in ("I like 📊 Statistics", "🔔 Alerts please", "alerts"):
        parsed = parse_update({"message": {"chat": {"id": 1}, "text": text}}, command_limit=LIMIT)
        assert parsed.command is Command.UNKNOWN, text


@pytest.mark.parametrize(("name", "description"), BOT_COMMANDS)
def test_every_registered_command_is_one_the_parser_answers(name: str, description: str):
    """Telegram will happily advertise a command the bot ignores.

    `setMyCommands` puts these in the client's menu button and in
    autocomplete, so a registered command with no handler is a visible
    dead end. Checked here rather than trusted, because the two lists
    live in different files.
    """
    parsed = parse_update({"message": {"chat": {"id": 1}, "text": f"/{name}"}}, command_limit=LIMIT)

    assert parsed.command is not Command.UNKNOWN
    assert description.strip(), f"/{name} has no description"


def test_both_menus_are_reachable_by_command_as_well_as_by_button():
    """The keyboard is a convenience, never the only way in.

    A client that does not render one, a chat where it was hidden, or a
    user who types — all three have to work, which is what
    `setMyCommands` is for.
    """
    registered = {name for name, _ in BOT_COMMANDS}

    assert {"alerts", "stats"} <= registered


def test_the_button_labels_are_distinct():
    """Two buttons with one label would make one of them unreachable."""
    assert len(set(BUTTON_COMMANDS)) == len(BUTTON_COMMANDS)
