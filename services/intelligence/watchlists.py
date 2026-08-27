"""The three derived watchlists. Live queries, never a stored table.

## Source of truth, enforced by not having a second one

DOWN TREND, CONSOLIDATION and BREAKOUT READY are filters over
`market_state` and nothing else. Module 10 built `watchlist()` for exactly
this and stated the reason: a stored watchlist table would be a second
place a security's membership could be recorded, and the moment the two
disagreed there would be no way to say which was right.

So this module calls Module 10's function. It does not keep a copy, a
cache, or a "materialized for performance" table — the same reasoning that
makes `market_state` a projection of `market_state_transitions` rather
than an independent record.

The consequence a test checks: change a security's state and the list
changes on the very next request. There is no refresh step because there
is nothing to refresh.

## Exclusions come from the state, not from a filter here

`UNCLASSIFIED` appears on no list, and that is the point of the state
existing. A security Module 09 found ineligible is classified
`UNCLASSIFIED` by Module 10, so it is absent from all three lists without
this module filtering anything — which is stronger than filtering,
because a filter can be forgotten and an absent state cannot be.

`UPTREND` and `DISTRIBUTION` are likewise on no list. They are internal
states: one already broke out, one is showing topping characteristics.

## Score numbers, never fundamentals

An entry shows what ARGUS thinks — `argus_score`, `confidence`,
`opportunity_score`, `risk_score`, `probability`. Revenue and P/E belong
to the Terminal, which is a different question about the same company. An
AST test asserts nothing here can reach a fundamentals read.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.engine import Connection

from core.market_state.watchlists import WATCHLIST_NAMES, states_for, watchlist
from infra.db.schema.identity import security_identity, security_ticker_history
from services.intelligence.blocks import build_freshness, build_score
from services.intelligence.errors import UNKNOWN_WATCHLIST, IntelligenceError
from services.intelligence.reads import latest_signal, state_row
from services.intelligence.schemas import IntelligenceEntry, IntelligenceWatchlist

__all__ = ["WATCHLIST_NAMES", "read_watchlist"]


def read_watchlist(
    connection: Connection,
    name: str,
    *,
    as_of: datetime,
    stale_after_seconds: float,
    limit: int | None = None,
) -> IntelligenceWatchlist:
    """One derived watchlist, as it is at this instant.

    `limit` bounds the response, not the query's meaning: `count` is the
    full membership regardless, so a client showing the first twenty knows
    how many there are.
    """
    if name not in WATCHLIST_NAMES:
        raise IntelligenceError(
            UNKNOWN_WATCHLIST,
            f"No watchlist named {name!r}. ARGUS derives exactly three: "
            f"{', '.join(WATCHLIST_NAMES)}.",
            status=404,
            detail={"watchlist": name, "available": list(WATCHLIST_NAMES)},
        )

    # Module 10's own query over the state projection. Not reimplemented
    # here, and not cached — see the module docstring.
    members = watchlist(connection, name)
    shown = members if limit is None else members[:limit]

    tickers = _tickers(connection, shown, as_of=as_of)
    entries: list[IntelligenceEntry] = []
    for security_id in shown:
        state = state_row(connection, security_id)
        if state is None:  # pragma: no cover - the membership came from it
            continue
        identity = tickers.get(security_id, (None, None))
        entries.append(
            IntelligenceEntry(
                security_id=security_id,
                ticker=identity[0],
                name=identity[1],
                state=str(state.state),
                entered_at=state.entered_at,
                state_confidence=(
                    float(state.confidence) if state.confidence is not None else None
                ),
                score=build_score(latest_signal(connection, security_id)),
            )
        )

    newest = max((entry.entered_at for entry in entries), default=None)
    return IntelligenceWatchlist(
        name=name,
        states=sorted(state.value for state in states_for(name)),
        entries=entries,
        count=len(members),
        freshness=build_freshness(
            computed_at=newest, as_of=as_of, stale_after_seconds=stale_after_seconds
        ),
    )


def _tickers(
    connection: Connection, security_ids: list[UUID], *, as_of: datetime
) -> dict[UUID, tuple[str | None, str | None]]:
    """Current ticker and name per security, in one query.

    One query rather than one per entry: a watchlist can hold hundreds of
    securities and a per-row lookup would be the difference between one
    round trip and hundreds on the page a user opens first.
    """
    if not security_ids:
        return {}

    rows = connection.execute(
        select(
            security_identity.c.id,
            security_identity.c.name,
            security_ticker_history.c.ticker,
            security_ticker_history.c.valid_from,
        )
        .select_from(
            security_identity.outerjoin(
                security_ticker_history,
                (security_ticker_history.c.security_id == security_identity.c.id)
                & (security_ticker_history.c.valid_from <= as_of)
                & (
                    security_ticker_history.c.valid_to.is_(None)
                    | (security_ticker_history.c.valid_to > as_of)
                ),
            )
        )
        .where(security_identity.c.id.in_(security_ids))
        .order_by(desc(security_ticker_history.c.valid_from))
    ).all()

    resolved: dict[UUID, tuple[str | None, str | None]] = {}
    for row in rows:
        resolved.setdefault(row.id, (row.ticker, row.name))
    return resolved
