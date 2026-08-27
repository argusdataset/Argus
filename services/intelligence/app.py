"""The Intelligence HTTP surface. Routing and translation only.

Every route is three lines: read the request, call an assembly function,
return what it gives back. Nothing here computes, and nothing here decides
what a security is — those questions were answered by Modules 10 through
16 and stored.

## Identity comes from Module 19's seam, not from a second mechanism

Module 19 built `current_user_id` as the single place identity enters
ARGUS's HTTP layer, specifically so Module 22 can replace one function
body when real authentication arrives. This module depends on it rather
than growing its own — a second identity mechanism would mean two places
Module 22 has to find.

Today nothing here is user-scoped: the three derived watchlists are the
same for everyone, and a security's score is a property of the security.
`optional_user` exists so a route that needs scoping later has the seam
already wired, and so the dependency is visible rather than being
remembered when it is needed.

## Why this module is not gated the way Module 20 is

Module 20 gates aggregate public track-record claims — "this is how ARGUS
has performed" — behind a named human's approval, because such a claim is
a statement about the system's history that nobody should be able to
publish by accident.

This module shows **current evidence about one security to someone asking
about that security now**. "ARGUS classifies this as CONSOLIDATION and
cannot score it, because the historical-evidence component was
unmeasurable" is not a track-record claim; it is ARGUS reporting what it
currently holds, caveats attached. Gating it would mean a person could not
see what the system thinks until somebody approved the thought — and the
three watchlists have always been ungated live views of `market_state` for
the same reason.

The report states this reasoning in full, including where the line would
sit if a future endpoint crossed it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.market_state.watchlists import WATCHLIST_NAMES
from services.intelligence.cases import read_case
from services.intelligence.config import IntelligenceConfig
from services.intelligence.detail import read_detail
from services.intelligence.errors import SECURITY_NOT_FOUND, IntelligenceError
from services.intelligence.overlays import read_overlays
from services.intelligence.schemas import (
    CaseExplanation,
    IntelligenceWatchlist,
    OverlayResponse,
    SecurityDetail,
)
from services.intelligence.watchlists import read_watchlist
from services.terminal.identity import USER_HEADER, current_user_id

__all__ = ["create_app"]


def create_app(engine: Engine, config: IntelligenceConfig | None = None) -> FastAPI:
    settings = config or IntelligenceConfig()

    app = FastAPI(
        title="ARGUS Intelligence API",
        version="1",
        summary="What ARGUS currently thinks about a security, and why.",
        description=(
            "Assembles what Modules 08-16 produced: the three derived watchlists, a "
            "security's score breakdown, its historical analogues, its risk inputs, and "
            "the explanation behind all of it. Computes nothing — every figure here was "
            "written by an earlier module and is served with its provenance and its "
            "limits attached."
        ),
    )
    app.state.engine = engine
    app.state.config = settings

    @app.exception_handler(IntelligenceError)
    async def _error(_request: Request, error: IntelligenceError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content=error.payload())

    app.include_router(_watchlist_router())
    app.include_router(_security_router())
    app.include_router(_case_router())
    return app


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def get_connection(request: Request) -> Iterator[Connection]:
    engine: Engine = request.app.state.engine
    with engine.begin() as connection:
        yield connection


def get_config(request: Request) -> IntelligenceConfig:
    return request.app.state.config


def optional_user(
    request: Request,
    connection: Annotated[Connection, Depends(get_connection)],
    x_argus_user: Annotated[str | None, Header(alias=USER_HEADER)] = None,
) -> UUID | None:
    """Who is asking, when they said. None when they did not.

    Delegates to Module 19's `current_user_id` — the single seam Module 22
    will replace. Optional because nothing this module serves is
    user-scoped yet; wired anyway so the dependency is visible and a
    future personalised route has nothing to invent.
    """
    authorization = request.headers.get("Authorization")
    if not x_argus_user and not authorization:
        return None

    from services.terminal.config import TerminalConfig
    from services.terminal.errors import TerminalError

    try:
        return current_user_id(
            connection,
            x_argus_user,
            config=TerminalConfig(),
            authorization=authorization,
        )
    except TerminalError:
        # Nothing here is user-scoped yet, so a credential that does not
        # check out means "anonymous", not "refused". The moment a route
        # here becomes personalised this must stop swallowing — which is
        # why it catches `TerminalError` narrowly and is documented rather
        # than being a bare except somebody trusts.
        return None


ConnectionDep = Annotated[Connection, Depends(get_connection)]
ConfigDep = Annotated[IntelligenceConfig, Depends(get_config)]
UserDep = Annotated[UUID | None, Depends(optional_user)]
LimitDep = Annotated[int | None, Query(ge=1, description="Maximum entries to return.")]


# --------------------------------------------------------------------------
# The three derived watchlists
# --------------------------------------------------------------------------


def _watchlist_router() -> APIRouter:
    router = APIRouter(prefix="/intelligence/watchlists", tags=["watchlists"])

    @router.get("", response_model=list[str])
    def index() -> list[str]:
        """The three names ARGUS derives. The set is closed by Module 10."""
        return list(WATCHLIST_NAMES)

    @router.get("/{name}", response_model=IntelligenceWatchlist)
    def show(
        name: str,
        connection: ConnectionDep,
        config: ConfigDep,
        limit: LimitDep = None,
    ) -> IntelligenceWatchlist:
        """One derived watchlist, read live from `market_state`.

        Not cached, not stored. A security whose state changed appears or
        disappears on the very next request, because there is nothing
        between this endpoint and Module 10's projection.
        """
        return read_watchlist(
            connection,
            name.upper(),
            as_of=datetime.now(UTC),
            stale_after_seconds=config.settings.stale_after_seconds,
            limit=limit or int(config.settings.max_watchlist_entries),
        )

    return router


# --------------------------------------------------------------------------
# Per-security detail
# --------------------------------------------------------------------------


def _security_router() -> APIRouter:
    router = APIRouter(prefix="/intelligence/securities", tags=["securities"])

    @router.get("/{ticker}", response_model=SecurityDetail)
    def detail(
        ticker: str,
        connection: ConnectionDep,
        config: ConfigDep,
        _user: UserDep = None,
    ) -> SecurityDetail:
        """Everything ARGUS holds about this security, assembled.

        An `INSUFFICIENT_EVIDENCE` candidate gets this same response with
        the same blocks — the score block says it was not scored and why,
        and the explanation block carries Module 16's account of the
        refusal. It is a complete answer, not a degraded one, and today it
        is the common one.
        """
        moment = datetime.now(UTC)
        return read_detail(
            connection,
            _resolve(connection, ticker, moment),
            as_of=moment,
            stale_after_seconds=config.settings.stale_after_seconds,
        )

    @router.get("/{ticker}/overlays", response_model=OverlayResponse)
    def overlays(
        ticker: str,
        connection: ConnectionDep,
        config: ConfigDep,
        since: Annotated[
            int | None, Query(description="Unix seconds. Visible range start.")
        ] = None,
    ) -> OverlayResponse:
        """Chart overlays — state markers and consolidation spans. No bars.

        Extends Module 19's datafeed rather than duplicating it: the
        response points at `/terminal/datafeed/history` for price data and
        serves only what Module 19 declined to.
        """
        moment = datetime.now(UTC)
        return read_overlays(
            connection,
            _resolve(connection, ticker, moment),
            as_of=moment,
            stale_after_seconds=config.settings.stale_after_seconds,
            since=datetime.fromtimestamp(since, tz=UTC) if since else None,
        )

    return router


# --------------------------------------------------------------------------
# Why did this fail
# --------------------------------------------------------------------------


def _case_router() -> APIRouter:
    router = APIRouter(prefix="/intelligence/setups", tags=["setups"])

    @router.get("/{setup_id}/outcome", response_model=CaseExplanation)
    def outcome(setup_id: UUID, connection: ConnectionDep, config: ConfigDep) -> CaseExplanation:
        """What happened to a concluded setup, narrated by Module 16.

        Named for the outcome rather than for failure, because successes
        and failures take the identical path — Module 15 built its records
        so a failure is as complete as a success.
        """
        return read_case(
            connection,
            setup_id,
            as_of=datetime.now(UTC),
            stale_after_seconds=config.settings.stale_after_seconds,
        )

    return router


def _resolve(connection: Connection, ticker: str, as_of: datetime) -> UUID:
    """Ticker to identity, at an instant.

    Uses Module 05's resolver, which enforces the one-ticker-one-security
    rule with a database exclusion constraint. Tickers are recycled, so
    "who was trading as this symbol" genuinely has a date-dependent
    answer.
    """
    from data.normalization.identity import SecurityIdentityResolver

    security_id = SecurityIdentityResolver(connection).try_resolve(ticker, as_of)
    if security_id is None:
        raise IntelligenceError(
            SECURITY_NOT_FOUND,
            f"No security is trading as {ticker!r}.",
            status=404,
            detail={"ticker": ticker},
        )
    return security_id
