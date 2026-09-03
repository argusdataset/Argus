"""Module 27 — Telegram alerts on BREAKOUT_READY, with two menus.

An open bot: anyone can `/start` it, no ARGUS account required. Three
parts — a webhook service that subscribes, unsubscribes and answers two
menus; a cron that reads Module 10's transition log and sends alerts; and
a one-shot entrypoint that registers the command menu with Telegram.

The Statistics menu calls `public_stats`'s own API over Railway's private
network rather than recomputing anything, so the bot and the public page
cannot disagree about what ARGUS has published.

See README.md for the two one-time operator commands, and for what this
module deliberately does not say in a message.
"""

from services.telegram.config import TelegramConfig, TelegramSetting, TelegramSettings
from services.telegram.endpoint import StatsEndpoint
from services.telegram.stats import StatsSnapshot, StatsState

__all__ = [
    "StatsEndpoint",
    "StatsSnapshot",
    "StatsState",
    "TelegramConfig",
    "TelegramSetting",
    "TelegramSettings",
]
