"""The three official watchlists, as queries over `market_state`.

```
DOWN TREND      = state IN (DOWN_TREND, BASE_FORMING)
CONSOLIDATION   = state IN (CONSOLIDATION, ACCUMULATION)
BREAKOUT READY  = state IN (BREAKOUT_WATCH, BREAKOUT_READY)
```

**Never stored.** Per the Source-of-Truth principle, a watchlist is a
filter over the state projection and nothing else. A stored watchlist
table would be a second place a security's membership could be recorded,
and the moment the two disagreed there would be no way to say which was
right — the same reasoning that makes `market_state` a projection of
`market_state_transitions` rather than an independent record.

`UPTREND` and `DISTRIBUTION` appear on no list. They are internal states:
a security that already broke out, or one showing topping characteristics.
Both inform transition logic and same-asset history without being
surfaced as a public list of their own.

`UNCLASSIFIED` appears on no list either, which is the point of having it.
A security Module 09 found ineligible, or one with too little history,
must be absent rather than defaulted into a list — otherwise every
watchlist quietly accumulates securities nobody ever judged.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.market_state.states import WATCHLISTS
from infra.db.enums import MarketState
from infra.db.schema.intelligence import market_state

#: The public names, in the order a UI would show them.
WATCHLIST_NAMES: tuple[str, ...] = ("DOWN_TREND", "CONSOLIDATION", "BREAKOUT_READY")


def states_for(name: str) -> frozenset[MarketState]:
    """Which states a named watchlist covers."""
    if name not in WATCHLISTS:
        raise KeyError(f"Unknown watchlist {name!r}. Known: {sorted(WATCHLISTS)}")
    return WATCHLISTS[name]


def watchlist(connection: Connection, name: str) -> list[UUID]:
    """Securities currently on one watchlist, read from the projection."""
    states = states_for(name)
    rows = (
        connection.execute(
            select(market_state.c.security_id)
            .where(market_state.c.state.in_([s.value for s in states]))
            .order_by(market_state.c.entered_at.desc())
        )
        .scalars()
        .all()
    )
    return list(rows)


def all_watchlists(connection: Connection) -> dict[str, list[UUID]]:
    """Every public watchlist in one call."""
    return {name: watchlist(connection, name) for name in WATCHLIST_NAMES}
