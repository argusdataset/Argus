"""Module 27 — Telegram alerts on BREAKOUT_READY.

An open bot: anyone can `/start` it, no ARGUS account required, and gets
one message when a security enters the BREAKOUT_READY watchlist. Two
halves — a webhook service that only ever subscribes and unsubscribes,
and a cron that reads Module 10's transition log and sends.

See README.md for the setWebhook command an operator runs once, and for
what this module deliberately does not say in a message.
"""

from services.telegram.config import TelegramConfig, TelegramSetting, TelegramSettings

__all__ = ["TelegramConfig", "TelegramSetting", "TelegramSettings"]
