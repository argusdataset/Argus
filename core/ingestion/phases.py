"""Which watchlist phase each security is in *right now*.

Read live from the `market_state` projection at decision time, every
time. That is not a performance note, it is the constraint
`core/market_state/watchlists.py` states as the Source-of-Truth
principle: a watchlist is a filter over the state projection and never an
independently stored value, because a second stored copy of "current
state" is exactly the thing that quietly disagrees with the real one.

This module's own table stores which phase drove a *completed refresh* —
a historical fact about that event. Nothing anywhere treats that stored
phase as an answer to "what phase is this security in now". The question
is asked here, against the projection, on every run.

## The state -> watchlist map is Module 10's

Read from `WATCHLISTS` rather than restated. A state added to a list in
Module 10 appears here with no change, and a state on no list
(`UNCLASSIFIED`, `DISTRIBUTION`) correctly yields nothing — which
`tiers.decide` turns into "no tier, not due".

## If a state ever appeared on two lists

It does not today; the four lists are disjoint. Should that change, the
most urgent tier wins — the shortest interval — because the alternative
is a dictionary-ordering coin flip deciding how often a security gets
looked at. Defined here rather than left to chance, and asserted in the
tests.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.ingestion.config import TIER_SETTINGS, IngestionSettings
from core.market_state.states import WATCHLISTS
from infra.db.enums import MarketState
from infra.db.schema.intelligence import market_state

__all__ = ["current_phases", "watchlist_for_state"]


def watchlist_for_state(
    state: MarketState | str,
    settings: IngestionSettings | None = None,
) -> str | None:
    """The refresh tier's watchlist name for a market state, or None.

    None for a state on no list, and also for a state whose list has no
    tier configured — the second cannot happen while the tier map covers
    Module 10's names, which a test asserts.
    """
    value = state.value if isinstance(state, MarketState) else str(state)
    resolved = settings or IngestionSettings()

    matches = [
        name
        for name, states in WATCHLISTS.items()
        if name in TIER_SETTINGS and any(member.value == value for member in states)
    ]
    if not matches:
        return None
    return min(matches, key=resolved.interval_days)


def current_phases(
    connection: Connection,
    security_ids: list[UUID],
    settings: IngestionSettings | None = None,
) -> dict[UUID, tuple[MarketState, str]]:
    """Each security's current state and tier, for those that have a tier.

    Absent from the result means either no `market_state` row at all or a
    state on no watchlist. Both are "not due", and `tiers.decide` says so
    in the same words for both because the consequence is identical.

    One query for the whole universe rather than one per security: the
    projection holds one row per security and this run needs all of them.
    """
    if not security_ids:
        return {}

    resolved = settings or IngestionSettings()
    rows = connection.execute(
        select(market_state.c.security_id, market_state.c.state).where(
            market_state.c.security_id.in_(security_ids)
        )
    ).all()

    phases: dict[UUID, tuple[MarketState, str]] = {}
    for row in rows:
        state = MarketState(row.state)
        watchlist = watchlist_for_state(state, resolved)
        if watchlist is not None:
            phases[row.security_id] = (state, watchlist)
    return phases
