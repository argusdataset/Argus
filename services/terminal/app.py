"""The HTTP surface. Routing and translation only — no logic lives here.

## What this file does and does not do

Every route below is three lines: read the request, call a service
function, return what it gives back. The service functions in
`company.py`, `news.py`, `datafeed.py`, `watchlists.py` and
`freshness.py` know nothing about HTTP — they take a connection and
plain arguments and raise `TerminalError`.

That split is worth the indirection for one specific reason: it makes
every one of them testable without a client, a server, or a running
event loop, and it means a second transport (a background job, a CLI, a
future gRPC surface) can call the same code without going through HTTP to
reach it.

## Framework choice

FastAPI. The project already depends on Pydantic 2 and httpx, and
`pytest-asyncio` is already configured — the skeleton was assembled
expecting an async HTTP service tested with httpx, which is exactly what
FastAPI gives. The response models in `schemas.py` are the contract, and
FastAPI serves them and documents them from one definition rather than
two that can disagree.

## One error handler

`TerminalError` is translated in exactly one place, so every failure this
service produces has the same envelope. A route that wants a new failure
mode adds a code in `errors.py`; it does not build a response.

## Connections

`get_connection` yields a transaction per request and commits on the way
out. Read-heavy routes commit nothing, which costs nothing; the write
routes need it and having one rule is worth more than saving a no-op
commit.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from services.terminal import company, datafeed, freshness, news, watchlists
from services.terminal.config import TerminalConfig
from services.terminal.errors import TerminalError
from services.terminal.identity import USER_HEADER, current_user_id
from services.terminal.schemas import (
    BarsResponse,
    DatafeedConfig,
    FundamentalsResponse,
    NewsResponse,
    ScanStatusResponse,
    SymbolInfo,
    SymbolSearchResult,
    ValuationResponse,
    WatchlistDetail,
    WatchlistSummary,
)

__all__ = ["create_app"]


class WatchlistCreateRequest(BaseModel):
    name: str = Field(description="What the user calls this list.")


class WatchlistRenameRequest(BaseModel):
    name: str


class WatchlistTickerRequest(BaseModel):
    ticker: str
    position: int | None = Field(
        default=None, description="Where in the list. Appended when omitted."
    )


def create_app(engine: Engine, config: TerminalConfig | None = None) -> FastAPI:
    """Build the Terminal app against a database engine.

    The engine is injected rather than constructed here so a test can
    hand in a throwaway database and a deployment can hand in a pooled
    one, without either needing to know how the other does it.
    """
    settings = config or TerminalConfig()

    app = FastAPI(
        title="ARGUS Terminal API",
        version="1",
        summary="Company fundamentals, news, chart data and user watchlists.",
        description=(
            "Serves the Terminal: what a person sees when they look up a company. "
            "Deliberately separate from ARGUS's scoring pipeline — nothing here "
            "reads or feeds a signal, a score, or a market state."
        ),
    )
    app.state.engine = engine
    app.state.config = settings

    @app.exception_handler(TerminalError)
    async def _terminal_error(_request: Request, error: TerminalError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content=error.payload())

    app.include_router(_company_router())
    app.include_router(_datafeed_router())
    app.include_router(_watchlist_router())
    app.include_router(_status_router())
    return app


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def get_connection(request: Request) -> Iterator[Connection]:
    """One transaction per request, committed on the way out."""
    engine: Engine = request.app.state.engine
    with engine.begin() as connection:
        yield connection


def get_config(request: Request) -> TerminalConfig:
    return request.app.state.config


def get_user(
    request: Request,
    connection: Annotated[Connection, Depends(get_connection)],
    x_argus_user: Annotated[str | None, Header(alias=USER_HEADER)] = None,
) -> UUID:
    """Who is asking.

    The single place identity enters this service. Module 22 replaces
    `current_user_id`'s body and nothing here changes — see
    `identity.py`.
    """
    return current_user_id(connection, x_argus_user, config=request.app.state.config)


#: Query parameters shared by several routes. Declared once, as
#: `Annotated` aliases rather than defaults, so the call to `Query` is not
#: evaluated in a function signature — the FastAPI-idiomatic form, and the
#: one that does not trip B008.
AsOfDep = Annotated[
    datetime | None,
    Query(
        description=(
            "Point-in-time cutoff. Defaults to now. Every figure returned was knowable "
            "to ARGUS at this instant; nothing filed later is visible."
        )
    ),
]
LimitDep = Annotated[int | None, Query(ge=1, description="Maximum rows to return.")]

ConnectionDep = Annotated[Connection, Depends(get_connection)]
ConfigDep = Annotated[TerminalConfig, Depends(get_config)]
UserDep = Annotated[UUID, Depends(get_user)]


# --------------------------------------------------------------------------
# Company: fundamentals, valuation, news
# --------------------------------------------------------------------------


def _company_router() -> APIRouter:
    router = APIRouter(prefix="/terminal/companies", tags=["company"])

    @router.get("/{ticker}/fundamentals", response_model=FundamentalsResponse)
    def fundamentals(
        ticker: str,
        connection: ConnectionDep,
        as_of: AsOfDep = None,
    ) -> FundamentalsResponse:
        return company.read_fundamentals(connection, ticker, as_of=as_of)

    @router.get("/{ticker}/valuation", response_model=ValuationResponse)
    def valuation(
        ticker: str,
        connection: ConnectionDep,
        as_of: AsOfDep = None,
    ) -> ValuationResponse:
        return company.read_valuation(connection, ticker, as_of=as_of)

    @router.get("/{ticker}/news", response_model=NewsResponse)
    def company_news(
        ticker: str,
        connection: ConnectionDep,
        config: ConfigDep,
        as_of: AsOfDep = None,
        limit: LimitDep = None,
    ) -> NewsResponse:
        return news.read_news(connection, ticker, as_of=as_of, limit=limit, config=config)

    return router


# --------------------------------------------------------------------------
# Chart datafeed (TradingView UDF)
# --------------------------------------------------------------------------


def _datafeed_router() -> APIRouter:
    router = APIRouter(prefix="/terminal/datafeed", tags=["datafeed"])

    @router.get("/config", response_model=DatafeedConfig)
    def config_endpoint(config: ConfigDep) -> DatafeedConfig:
        return datafeed.datafeed_config(config)

    @router.get("/time")
    def time_endpoint() -> int:
        return datafeed.server_time()

    @router.get("/symbols", response_model=SymbolInfo)
    def symbols(symbol: str, connection: ConnectionDep) -> SymbolInfo:
        return datafeed.resolve_symbol(connection, symbol)

    @router.get("/search", response_model=list[SymbolSearchResult])
    def search(
        connection: ConnectionDep,
        config: ConfigDep,
        query: Annotated[str, Query(description="Prefix of a ticker, or part of a name.")] = "",
        limit: LimitDep = None,
        exchange: Annotated[str | None, Query()] = None,
    ) -> list[SymbolSearchResult]:
        return datafeed.search_symbols(
            connection, query, limit=limit, exchange=exchange, config=config
        )

    @router.get("/history", response_model=BarsResponse, response_model_by_alias=True)
    def history(
        connection: ConnectionDep,
        config: ConfigDep,
        symbol: str,
        resolution: str,
        from_: Annotated[int, Query(alias="from", description="Unix seconds, inclusive.")],
        to: Annotated[int, Query(description="Unix seconds, inclusive.")],
        countback: LimitDep = None,
    ) -> BarsResponse:
        # The protocol sends Unix seconds. Converting here rather than in
        # the service keeps the service's arguments ordinary datetimes.
        return datafeed.symbol_history(
            connection,
            symbol,
            resolution=resolution,
            start=datetime.fromtimestamp(from_, tz=UTC),
            end=datetime.fromtimestamp(to, tz=UTC),
            countback=countback,
            config=config,
        )

    return router


# --------------------------------------------------------------------------
# User watchlists
# --------------------------------------------------------------------------


def _watchlist_router() -> APIRouter:
    router = APIRouter(prefix="/terminal/watchlists", tags=["watchlists"])

    @router.get("", response_model=list[WatchlistSummary])
    def index(connection: ConnectionDep, user_id: UserDep) -> list[WatchlistSummary]:
        return watchlists.list_watchlists(connection, user_id)

    @router.post("", response_model=WatchlistDetail, status_code=201)
    def create(
        body: WatchlistCreateRequest,
        connection: ConnectionDep,
        config: ConfigDep,
        user_id: UserDep,
    ) -> WatchlistDetail:
        return watchlists.create_watchlist(connection, user_id, body.name, config=config)

    @router.get("/{watchlist_id}", response_model=WatchlistDetail)
    def read(watchlist_id: UUID, connection: ConnectionDep, user_id: UserDep) -> WatchlistDetail:
        return watchlists.read_watchlist(connection, user_id, watchlist_id)

    @router.patch("/{watchlist_id}", response_model=WatchlistDetail)
    def rename(
        watchlist_id: UUID,
        body: WatchlistRenameRequest,
        connection: ConnectionDep,
        config: ConfigDep,
        user_id: UserDep,
    ) -> WatchlistDetail:
        return watchlists.rename_watchlist(
            connection, user_id, watchlist_id, body.name, config=config
        )

    @router.delete("/{watchlist_id}", status_code=204)
    def destroy(watchlist_id: UUID, connection: ConnectionDep, user_id: UserDep) -> None:
        watchlists.delete_watchlist(connection, user_id, watchlist_id)

    @router.post("/{watchlist_id}/items", response_model=WatchlistDetail)
    def add_item(
        watchlist_id: UUID,
        body: WatchlistTickerRequest,
        connection: ConnectionDep,
        config: ConfigDep,
        user_id: UserDep,
    ) -> WatchlistDetail:
        return watchlists.add_security(
            connection,
            user_id,
            watchlist_id,
            body.ticker,
            position=body.position,
            config=config,
        )

    @router.delete("/{watchlist_id}/items/{ticker}", response_model=WatchlistDetail)
    def remove_item(
        watchlist_id: UUID, ticker: str, connection: ConnectionDep, user_id: UserDep
    ) -> WatchlistDetail:
        return watchlists.remove_security(connection, user_id, watchlist_id, ticker)

    return router


# --------------------------------------------------------------------------
# Data freshness
# --------------------------------------------------------------------------


def _status_router() -> APIRouter:
    router = APIRouter(prefix="/terminal", tags=["status"])

    @router.get("/scan-status/{scan_date}", response_model=ScanStatusResponse)
    def scan_status(scan_date: date, connection: ConnectionDep) -> ScanStatusResponse:
        """Whether ARGUS scanned this date. Freshness metadata, not intelligence.

        Routed through Module 18's `scan_results`, which resolves the
        calendar date through `live_scan_runs`. Nothing here reconstructs
        a session-close offset — see `freshness.py`.
        """
        return freshness.read_scan_status(connection, scan_date)

    return router
