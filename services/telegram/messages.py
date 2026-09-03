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
from services.telegram.stats import ChartSummary, StatsSnapshot, StatsState

__all__ = [
    "ALERT_FOOTER",
    "STATS_FOOTER",
    "MENU_TEXT",
    "START_TEXT",
    "STOP_TEXT",
    "SUBSCRIBED_TEXT",
    "UNSUBSCRIBED_TEXT",
    "alert_text",
    "alerts_menu_text",
    "statistics_text",
    "truncate",
]

#: Attached to every alert, not only the welcome message. See the module
#: docstring on why.
ALERT_FOOTER = "A state change, not a recommendation. ARGUS has no validated track record."

#: The statistics footer says something different, and has to. Once a
#: number is on screen, "ARGUS has no validated track record" is no
#: longer true of the message it is attached to — a disclaimer that
#: contradicts the text above it teaches a reader to skip disclaimers.
STATS_FOOTER = (
    "Published under ARGUS's review gate, not a recommendation. "
    "Past outcomes do not predict future ones."
)

START_TEXT = (
    "Subscribed.\n\n"
    "You'll get one message when a security enters the BREAKOUT_READY watchlist — "
    "a state ARGUS assigns from measured price structure, nothing more.\n\n"
    "This is not investment advice and ARGUS has no validated track record. "
    "It is an experiment in building one honestly.\n\n"
    "Send /stop to unsubscribe."
)

STOP_TEXT = "Unsubscribed. Send /start if you want the alerts back."

MENU_TEXT = "Pick a menu."

SUBSCRIBED_TEXT = "Subscribed. You'll get a message on each BREAKOUT_READY entry."
UNSUBSCRIBED_TEXT = "Unsubscribed. No more alerts until you subscribe again."

#: Where the actual charts are. The bot summarises; it does not draw.
PUBLIC_PAGE_DEFAULT = "https://publicstats-production.up.railway.app"

#: Chart name -> how to render its summary, in `V1_CHARTS` order.
_CHART_TITLES = {
    "win_rate": "Win rate",
    "cumulative_performance": "Cumulative performance",
}


def alerts_menu_text(*, subscribed: bool) -> str:
    """The Alerts menu. State first, because that is what was asked.

    Read from `telegram_subscribers` by the caller — there is no second
    copy of "is this chat subscribed" anywhere, which is why this takes a
    bool rather than a chat id.
    """
    if subscribed:
        return (
            "🔔 Alerts: subscribed.\n\n"
            "You get one message when a security enters the BREAKOUT_READY "
            "watchlist."
        )
    return (
        "🔔 Alerts: not subscribed.\n\n"
        "Subscribe to get one message when a security enters the BREAKOUT_READY "
        "watchlist."
    )


def statistics_text(snapshot: StatsSnapshot, *, public_page: str = PUBLIC_PAGE_DEFAULT) -> str:
    """ARGUS's published track record, or an honest account of its absence.

    Four outcomes, deliberately four messages. Collapsing "nothing has
    been approved" into "no data" would let a reader take an absence of
    permission for an absence of results; collapsing "cannot reach the
    service" into either would let a container restart read as a claim
    about the track record.
    """
    if snapshot.state is StatsState.UNREACHABLE:
        return (
            "📊 Statistics are temporarily unavailable — ARGUS's public "
            "statistics service could not be reached just now.\n\n"
            "This says nothing about the track record itself. Try again shortly, "
            f"or read the page directly: {public_page}"
        )

    if snapshot.state is StatsState.NOT_PUBLISHED:
        lines = ["📊 ARGUS has not published statistics yet."]
        if snapshot.explanation:
            # `public_stats`'s own words rather than a paraphrase, so the
            # bot and the page cannot drift into saying different things.
            lines.append("")
            lines.append(snapshot.explanation)
        lines.extend(_release_lines(snapshot))
        lines.append("")
        lines.append(public_page)
        return "\n".join(lines)

    lines = ["📊 ARGUS's published track record"]
    for chart in snapshot.charts:
        lines.append("")
        lines.extend(_chart_lines(chart))

    lines.extend(_release_lines(snapshot))
    lines.append("")
    lines.append(f"Charts and full detail: {public_page}")
    lines.append("")
    lines.append(STATS_FOOTER)
    return "\n".join(lines)


def _chart_lines(chart: ChartSummary) -> list[str]:
    """One chart as text. Never a number the API did not give.

    A chart under the sample floor reports the floor, not a rate computed
    from too little — which is the whole point of `public_stats` carrying
    an `unavailable` block instead of a small number.
    """
    title = _CHART_TITLES.get(chart.chart, chart.chart.replace("_", " ").capitalize())

    if not chart.available:
        observed = chart.observed if chart.observed is not None else chart.sample_size
        required = chart.required
        detail = (
            f"{observed} of {required} outcomes required"
            if required is not None
            else f"{observed} outcome(s) so far"
        )
        return [f"{title}: not yet reportable — {detail}."]

    lines = [f"{title}:"]
    if chart.chart == "win_rate":
        lines.extend(_win_rate_lines(chart))
    elif chart.chart == "cumulative_performance":
        lines.extend(_cumulative_lines(chart))
    lines.append(f"  Sample: {chart.sample_size}")
    return lines


def _win_rate_lines(chart: ChartSummary) -> list[str]:
    lines = []
    hit_rate = chart.values.get("hit_rate")
    precision = chart.values.get("precision")
    if isinstance(hit_rate, int | float):
        lines.append(f"  Hit rate: {float(hit_rate):.1%}")
    if isinstance(precision, int | float):
        lines.append(f"  Precision: {float(precision):.1%}")
    resolved = chart.values.get("resolved")
    if isinstance(resolved, int):
        lines.append(f"  Resolved outcomes: {resolved}")
    return lines


def _cumulative_lines(chart: ChartSummary) -> list[str]:
    """Final return and max drawdown — the two numbers a curve summarises.

    Not an ASCII rendering of the series. A curve drawn in monospace on a
    phone is a worse picture than the sentence "the page has the chart",
    and it invites reading a shape into resolution that is not there.
    """
    lines = []
    final = chart.values.get("final_cumulative_relative_return")
    drawdown = chart.values.get("max_drawdown")
    if isinstance(final, int | float):
        lines.append(f"  Final vs benchmark: {float(final):+.1%}")
    if isinstance(drawdown, int | float):
        lines.append(f"  Max drawdown: {float(drawdown):.1%}")
    basis = chart.values.get("basis")
    if isinstance(basis, str) and basis:
        lines.append(f"  Basis: {basis}")
    return lines


def _release_lines(snapshot: StatsSnapshot) -> list[str]:
    """Whether a human has approved anything, and the most recent decision.

    Count and latest only. The full table is public at the page — see
    `/public/releases` — and reproducing it here would be a spreadsheet
    in a chat window.
    """
    releases = snapshot.releases
    if releases is None:
        return []

    lines = [""]
    if releases.count == 0:
        lines.append("Review decisions: none yet.")
        return lines

    latest = f"Review decisions: {releases.count}"
    if releases.latest_status:
        latest += f" (most recent: {releases.latest_status}"
        if releases.latest_period_end:
            latest += f", through {releases.latest_period_end}"
        latest += ")"
    lines.append(latest + ".")
    return lines


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
