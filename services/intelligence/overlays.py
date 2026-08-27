"""ARGUS-specific chart overlays. Bars come from Module 19; none are served here.

## How this extends Module 19's datafeed rather than duplicating it

Module 19 built the TradingView datafeed — `/config`, `/time`, `/symbols`,
`/search`, `/history` — and deliberately advertised `supports_marks:
false`, stating the reason in its own module docstring: ARGUS's overlays
are Module 21's concern, and a datafeed that claims to serve marks and
returns none makes the widget request them forever.

So this module serves exactly the two endpoints Module 19 left out —
`/marks` and `/timescale_marks` — in the shapes the Charting Library
expects, and serves **no price data at all**. `OverlayResponse` carries a
`bars_endpoint` pointing at Module 19's `/history` instead.

That is the whole layering. A structural test asserts it holds: nothing in
this service imports `services.terminal.bars`, `load_ohlcv_panel_as_of`,
or any other price loader. A second OHLCV path would be a second price
series that could disagree with the first, and a chart showing one set of
bars with another set of marks on it would be worse than no chart.

**One thing a deployment must do, flagged rather than fixed:** Module 19's
`/config` still says `supports_marks: false`, and changing it would mean
editing Module 19 — outside this module's boundary. A deployment that
wants marks on the chart has to serve both services behind one datafeed
prefix and flip that flag. See the module report.

## What the overlays actually are

**State-transition markers** come from `market_state_transitions` — a
stored row per change, with its timestamp, the states either side, and the
evidence Module 10 recorded. Read, not derived.

**Consolidation zones** are the time spans during which the security was
in a consolidation state, reconstructed from the same transition log by
pairing each entry into a consolidation state with the exit that followed
it. That is reading a log, not computing a feature.

**Price boundaries for those zones do not exist**, and this module will
not invent them. No module stores the support/resistance levels that
would define a zone's top and bottom: Module 08's feature set carries
`normalized_range_width`, `support_test_count` and `resistance_test_count`
— a normalized width and two counts, none of which is a price. Deriving
levels here would be computing a feature in an API layer, which is
exactly the reach past the boundary this module is told to avoid. So the
response carries an `Unavailable` saying so, and the gap is reported
upstream.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Connection

from core.market_state.states import WATCHLISTS
from infra.db.enums import MarketState
from services.intelligence.blocks import build_freshness
from services.intelligence.detail import resolve_identity
from services.intelligence.reads import transitions_for
from services.intelligence.schemas import OverlayResponse, Unavailable

__all__ = ["BARS_ENDPOINT", "CONSOLIDATION_STATES", "read_overlays"]

#: Module 19's datafeed. Named here so the response can point at it, and
#: so a test can assert this module offers no alternative.
BARS_ENDPOINT = "/terminal/datafeed/history"

#: The states that make up a consolidation zone. Read from Module 10's own
#: watchlist mapping rather than restated, so the two cannot drift.
CONSOLIDATION_STATES: frozenset[MarketState] = WATCHLISTS["CONSOLIDATION"]

_ZONE_PRICE_EXPLANATION = (
    "ARGUS does not store price levels for a consolidation zone. Module 08 measures "
    "the base's normalized range width and how often support and resistance were "
    "tested, but no module records the prices themselves — so the zone is shown as a "
    "time span. Deriving levels here would mean computing a feature in an API layer, "
    "which would produce a number no other part of ARGUS could reproduce."
)

#: TradingView mark colours. Presentation, not judgement: a transition
#: into a consolidation state and one out of it are the same kind of
#: event, and the colour only helps a reader scan the chart.
_ENTER = "#2962FF"
_EXIT = "#787B86"


def read_overlays(
    connection: Connection,
    security_id: UUID,
    *,
    as_of: datetime,
    stale_after_seconds: float,
    since: datetime | None = None,
) -> OverlayResponse:
    """Marks and consolidation spans for one security. No bars.

    `since` bounds the transition history the way the Charting Library
    bounds a `getMarks` call — it asks for the visible range, not for
    everything.
    """
    ticker, _name = resolve_identity(connection, security_id, as_of=as_of)
    transitions = [
        row
        for row in transitions_for(connection, security_id, since=since)
        if row.transition_time <= as_of
    ]

    return OverlayResponse(
        security_id=security_id,
        ticker=ticker,
        bars_endpoint=BARS_ENDPOINT,
        marks=[_mark(index, row) for index, row in enumerate(transitions)],
        consolidation_zones=_zones(transitions, as_of=as_of),
        zone_price_boundaries=Unavailable(reason="not_stored", explanation=_ZONE_PRICE_EXPLANATION),
        freshness=build_freshness(
            computed_at=transitions[-1].transition_time if transitions else None,
            as_of=as_of,
            stale_after_seconds=stale_after_seconds,
        ),
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _mark(index: int, row: Any) -> dict[str, Any]:
    """One state change, in the Charting Library's mark shape.

    Field names are the library's, not ARGUS's — `labelFontColor` and
    `minSize` look wrong beside the rest of this codebase and are what the
    widget reads.
    """
    to_state = str(row.to_state)
    from_state = str(row.from_state) if row.from_state else None
    entering = to_state in {state.value for state in CONSOLIDATION_STATES}

    return {
        "id": index,
        "time": int(row.transition_time.timestamp()),
        "color": _ENTER if entering else _EXIT,
        "text": (f"{from_state} to {to_state}" if from_state else f"First classified {to_state}"),
        "label": to_state[0],
        "labelFontColor": "#FFFFFF",
        "minSize": 14,
        # ARGUS's own fields, alongside the library's. A client that only
        # renders the mark ignores them; one showing a tooltip has the
        # state either side and Module 10's confidence without a second
        # request.
        "argus": {
            "from_state": from_state,
            "to_state": to_state,
            "confidence": float(row.confidence) if row.confidence is not None else None,
            "backward_transition": bool((row.evidence or {}).get("backward_transition")),
        },
    }


def _zones(transitions: list[Any], *, as_of: datetime) -> list[dict[str, Any]]:
    """Time spans the security spent consolidating, paired from the log.

    Each entry into a consolidation state opens a span; the next
    transition out of one closes it. A span still open at `as_of` is
    returned with `end` null and `ongoing: true` rather than being closed
    at the cutoff — a zone that has not ended has not ended, and stamping it
    with today's date would assert a transition that never happened.
    """
    names = {state.value for state in CONSOLIDATION_STATES}
    zones: list[dict[str, Any]] = []
    started: datetime | None = None
    entered_state: str | None = None

    for row in transitions:
        to_state = str(row.to_state)
        if to_state in names:
            if started is None:
                started = row.transition_time
                entered_state = to_state
            continue
        if started is not None:
            zones.append(_zone(started, row.transition_time, entered_state, open_ended=False))
            started, entered_state = None, None

    if started is not None:
        zones.append(_zone(started, None, entered_state, open_ended=True))
    return zones


def _zone(
    start: datetime, end: datetime | None, state: str | None, *, open_ended: bool
) -> dict[str, Any]:
    return {
        "start": int(start.timestamp()),
        "end": int(end.timestamp()) if end is not None else None,
        "state": state,
        # `ongoing` rather than `open`: this object sits beside a price
        # chart, where `open` is the session's opening price. One word
        # meaning two things on the same screen is a bug waiting for a
        # careless reader.
        "ongoing": open_ended,
    }
