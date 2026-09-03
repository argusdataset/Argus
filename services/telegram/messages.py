"""The text ARGUS actually sends. Short, factual, and not a recommendation.

## What this copy is allowed to say

A state change happened. That is the whole claim, and it is one ARGUS can
stand behind: `BREAKOUT_READY` is a classification Module 10 made from
measured features, recorded in an append-only log, reproducible from the
target-model version stamped on the row.

## What it is not allowed to say

No score, no probability, no target, no "buy", "opportunity", "signal to
act", or any construction that reads as advice. ARGUS has no validated
track record — Module 20 gates aggregate performance claims behind a
named human's approval precisely because such a claim is a statement
about the system's history that nobody should publish by accident — and a
push notification is the *least* supervised surface in the project. A
sentence that would need Module 20's gate on the public page does not
become acceptable because it fits in a phone banner.

The same restraint the public-facing copy already follows: state what
happened, name the limits, and let the reader decide.

## Why the disclaimer is on the message and not just in `/start`

A user who subscribed months ago sees only this. Putting the qualifier
only in the welcome text would mean the caveat is read once and the
unqualified claim is read every day after, which is the arrangement that
makes disclaimers meaningless.
"""

from __future__ import annotations

from datetime import date

from core.data_validation.identity import SecurityLabel

__all__ = ["ALERT_FOOTER", "START_TEXT", "STOP_TEXT", "alert_text", "truncate"]

#: Attached to every alert, not only the welcome message. See the module
#: docstring on why.
ALERT_FOOTER = "A state change, not a recommendation. ARGUS has no validated track record."

START_TEXT = (
    "Subscribed.\n\n"
    "You'll get one message when a security enters the BREAKOUT_READY watchlist — "
    "a state ARGUS assigns from measured price structure, nothing more.\n\n"
    "This is not investment advice and ARGUS has no validated track record. "
    "It is an experiment in building one honestly.\n\n"
    "Send /stop to unsubscribe."
)

STOP_TEXT = "Unsubscribed. Send /start if you want the alerts back."


def alert_text(label: SecurityLabel, *, scan_date: date) -> str:
    """One alert. Two facts and a caveat.

    The session date is named rather than "today", because a dispatch run
    reports the session the scanner scanned, which at the hour this runs
    is the previous trading day. "Today" would be wrong roughly always.
    """
    return (
        f"{label.display()} entered BREAKOUT_READY.\n"
        f"Session: {scan_date.isoformat()}\n\n"
        f"{ALERT_FOOTER}"
    )


def truncate(text: str, *, limit: int) -> str:
    """Keep a message inside Telegram's own length limit.

    Telegram rejects an over-long body outright, so the choice is between
    truncating and dropping the message. Truncating, with a visible
    marker: an alert missing its footer is worse than no alert, but an
    alert that never arrived is worse than either.
    """
    if len(text) <= limit:
        return text
    marker = "…"
    return text[: max(limit - len(marker), 0)] + marker
