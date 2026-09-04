"""The four derived watchlists. Live queries, never a stored table.

## Source of truth, enforced by not having a second one

DOWN TREND, CONSOLIDATION, BREAKOUT READY and UPTREND are filters over
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
`UNCLASSIFIED` by Module 10, so it is absent from all four lists without
this module filtering anything — which is stronger than filtering,
because a filter can be forgotten and an absent state cannot be.

`DISTRIBUTION` is likewise on no list. It is an internal state — a
security showing topping characteristics — that informs transition logic
and same-asset history without being surfaced as a public list of its own.

## UPTREND: the one watchlist that shows lineage

The other three answer "what is ARGUS watching right now". `UPTREND`
answers a different question: "what did ARGUS call correctly, that is now
visibly moving". A security lands here only after confirming a breakout
ARGUS flagged earlier in its own DOWN_TREND, CONSOLIDATION and
BREAKOUT_READY history — so this is the one list where that history is
attached to each entry (`phase_history`), read from Module 10's
already-existing append-only transition log via
`services.intelligence.reads.transitions_for`. Nothing is computed here:
every transition was recorded when it happened.

`mfe` travels alongside it, read from Module 15's `setup_outcomes` when
the linked setup has concluded. A security still actively in `UPTREND`
typically has no concluded outcome yet, and this module reports that
honestly as `None` rather than computing a live, unvalidated figure — the
same "absence over invention" discipline every other public number in
ARGUS follows.

Like the other three, this is a live view: a security that reverses back
toward `DOWN_TREND` leaves it on the next request, the same as it would
leave any other watchlist. Its full history stays queryable through
Module 10's transition log regardless, and its full CASE record through
Module 15's once the setup concludes.

## Score numbers, never fundamentals

An entry shows what ARGUS thinks — `argus_score`, `confidence`,
`opportunity_score`, `risk_score`, `probability`. Revenue and P/E belong
to the Terminal, which is a different question about the same company. An
AST test asserts nothing here can reach a fundamentals read.

## `news_signal_raised` changes nothing above it

Module 28's reading is looked up after `watchlist()` has already decided
membership and after each entry's state and score are already built, and
it is attached to the entry as one more field — never as a filter, a
sort key, or an input to anything computed above. A security with no
stored reading gets `None`, the same "undetermined" value Module 28 uses
internally, and appears on the list exactly as it would if this lookup
were deleted entirely.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.engine import Connection

from core.market_state.watchlists import WATCHLIST_NAMES, states_for, watchlist
from infra.db.schema.identity import security_identity, security_ticker_history
from infra.db.schema.setups import setup_outcomes, setups
from services.intelligence.blocks import build_freshness, build_score
from services.intelligence.errors import UNKNOWN_WATCHLIST, IntelligenceError
from services.intelligence.reads import (
    latest_news_signals,
    latest_signal,
    state_row,
    transitions_for,
)
from services.intelligence.schemas import IntelligenceEntry, IntelligenceWatchlist, PhaseTransition

__all__ = ["CONFIRMED_MOVES_WATCHLIST", "WATCHLIST_NAMES", "read_watchlist"]

#: The one watchlist that carries phase history and MFE on each entry.
#: See the module docstring.
CONFIRMED_MOVES_WATCHLIST = "UPTREND"


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
            f"No watchlist named {name!r}. ARGUS derives exactly four: "
            f"{', '.join(WATCHLIST_NAMES)}.",
            status=404,
            detail={"watchlist": name, "available": list(WATCHLIST_NAMES)},
        )

    # Module 10's own query over the state projection. Not reimplemented
    # here, and not cached — see the module docstring.
    members = watchlist(connection, name)
    shown = members if limit is None else members[:limit]

    include_lineage = name == CONFIRMED_MOVES_WATCHLIST
    tickers = _tickers(connection, shown, as_of=as_of)
    # Module 28's readings, in one query rather than one per entry — see
    # `_tickers` on why a per-row lookup does not scale to a full list.
    # Purely additive: a security absent here still appears with
    # `news_signal_raised=None`, and nothing about membership or score
    # above depends on this lookup at all.
    news_signals = latest_news_signals(connection, shown)
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
                phase_history=(
                    _phase_history(connection, security_id) if include_lineage else None
                ),
                mfe=_current_mfe(connection, security_id) if include_lineage else None,
                news_signal_raised=(
                    news_signals[security_id].raised if security_id in news_signals else None
                ),
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


def _phase_history(connection: Connection, security_id: UUID) -> list[PhaseTransition]:
    """Every recorded state change for one security, oldest first.

    Reuses `transitions_for` — already built for `overlays.py`'s chart
    marks, reading the same `market_state_transitions` log this function
    would otherwise reimplement. Nothing computed: each field is the value
    Module 10 wrote when the transition happened.
    """
    return [
        PhaseTransition(
            from_state=str(row.from_state) if row.from_state else None,
            to_state=str(row.to_state),
            transition_time=row.transition_time,
            duration_in_prior_state_seconds=(
                row.duration_in_prior_state.total_seconds()
                if row.duration_in_prior_state is not None
                else None
            ),
            confidence=float(row.confidence) if row.confidence is not None else None,
        )
        for row in transitions_for(connection, security_id)
    ]


def _current_mfe(connection: Connection, security_id: UUID) -> float | None:
    """The maximum favourable excursion of this security's most recent setup.

    `None` whenever that setup has not concluded — which is the normal
    case for a security still actively sitting in `UPTREND`. Module 15
    only measures MFE once an outcome is recorded (`record_outcome` in
    `core/outcome_tracking/engine.py`); there is no live, on-demand
    excursion computation anywhere in ARGUS, and this function does not
    add one. Inventing a number here would mean this read-only module
    computing something for the first time, which is exactly the
    boundary Module 21's own tests hold it to.
    """
    row = connection.execute(
        select(setup_outcomes.c.mfe)
        .select_from(setups.outerjoin(setup_outcomes, setup_outcomes.c.setup_id == setups.c.id))
        .where(setups.c.security_id == security_id)
        .order_by(desc(setups.c.detected_at))
        .limit(1)
    ).one_or_none()
    if row is None or row.mfe is None:
        return None
    return float(row.mfe)
