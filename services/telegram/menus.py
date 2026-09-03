"""The two menus, as a reply keyboard. Presentation only.

## Reply keyboard, not inline — and the reason is a security property

An inline keyboard's buttons produce `callback_query` updates, and
Telegram requires each one to be answered with `answerCallbackQuery` or
the client shows a spinner until it times out. A webhook response can
carry exactly **one** method call, so answering the callback *and*
sending the reply needs two API calls — which means the webhook service
would have to hold `TELEGRAM_BOT_TOKEN`.

That is the one thing `services/telegram/app.py` was built to avoid: the
public-facing service holds no outbound Telegram credential, so a
compromise of it cannot send messages as the bot. A prettier keyboard is
not worth trading that for.

A reply keyboard's buttons send ordinary text messages, which the
existing `/start` path already handles. One update in, one reply out, no
token, no second call. The cost is cosmetic — the buttons sit above the
user's keyboard rather than under the message — and the labels are
parsed as commands, which `commands.py` does alongside the slash forms.

## The keyboard is not the interface

`setMyCommands` registers `/alerts` and `/stats` as well, so both menus
work when the keyboard is hidden, on a client that does not render one,
or for somebody who types. The keyboard is a convenience over a command
surface, never the only way in — see `infra/deploy/telegram_commands.py`.
"""

from __future__ import annotations

from typing import Any

from services.telegram.commands import Command

__all__ = [
    "ALERTS_BUTTON",
    "BACK_BUTTON",
    "BOT_COMMANDS",
    "BUTTON_COMMANDS",
    "STATS_BUTTON",
    "SUBSCRIBE_BUTTON",
    "UNSUBSCRIBE_BUTTON",
    "alerts_keyboard",
    "main_keyboard",
]

ALERTS_BUTTON = "🔔 Alerts"
STATS_BUTTON = "📊 Statistics"
SUBSCRIBE_BUTTON = "✅ Subscribe"
UNSUBSCRIBE_BUTTON = "🔕 Unsubscribe"
BACK_BUTTON = "⬅️ Back"

#: Button label -> the command it means. A button is a way of typing a
#: command, not a second code path: `SUBSCRIBE_BUTTON` reaches the same
#: `subscribers.subscribe` that `/start` does.
BUTTON_COMMANDS: dict[str, Command] = {
    ALERTS_BUTTON: Command.ALERTS,
    STATS_BUTTON: Command.STATS,
    SUBSCRIBE_BUTTON: Command.SUBSCRIBE,
    UNSUBSCRIBE_BUTTON: Command.UNSUBSCRIBE,
    BACK_BUTTON: Command.MENU,
}

#: What `setMyCommands` registers, and what `/help`-style clients list.
#:
#: Every entry maps to a command the parser handles — a test asserts it,
#: because a registered command the bot ignores is a button that does
#: nothing, and Telegram will happily advertise one.
BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Show the menu"),
    ("alerts", "Manage BREAKOUT_READY alerts"),
    ("stats", "ARGUS's published track record"),
    ("stop", "Unsubscribe from alerts"),
)


def main_keyboard() -> dict[str, Any]:
    """The two menus.

    `resize_keyboard` so it does not take half the screen on a phone;
    `is_persistent` so it stays available rather than vanishing after one
    use, which is what makes it a menu rather than a prompt.
    """
    return _keyboard([[ALERTS_BUTTON, STATS_BUTTON]])


def alerts_keyboard(*, subscribed: bool) -> dict[str, Any]:
    """Subscribe or unsubscribe, whichever this chat can currently do.

    Only the applicable action is offered. Showing both would mean one of
    them is always a no-op, and a button that does nothing teaches a
    reader that the buttons are decorative.
    """
    action = UNSUBSCRIBE_BUTTON if subscribed else SUBSCRIBE_BUTTON
    return _keyboard([[action], [BACK_BUTTON]])


def _keyboard(rows: list[list[str]]) -> dict[str, Any]:
    return {
        "keyboard": [[{"text": label} for label in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True,
    }
