"""The public HTTP surface. No auth, by design, and cacheable by construction.

## No authentication, deliberately

This is the one module in ARGUS that is public with no login. That is not
an oversight to be hardened later — it is the point. The project's
standing principle is *"don't manufacture demand, create undeniable
value"*, and this is the only place in the system where that becomes a
checkable fact rather than an intention: anyone, with no relationship to
ARGUS, can see how it has actually performed, including every failure.

Nothing here reads an identity, and there is no code path that could
serve a different answer to different callers.

## Cacheable by construction

Every chart response is a stored payload plus a freshness block. Reads
are one indexed lookup and two small gate queries — no aggregation
happens per request. `Cache-Control` is set from the snapshot's own age
against the configured refresh cadence, so an intermediary caches
something ARGUS has already decided is current rather than guessing.

The gate verification is not cached. It is the cheap query that stops a
withdrawn result being served, and caching it would defeat it.

## Two review endpoints, and why they are public reads

`GET /public/releases` publishes the review gate's own decisions. A gate
whose decisions were private would be a claim nobody could check, which
would leave this module asserting its own integrity rather than
demonstrating it. The reviewer's *identity* is not published — that a
named human decided is the verifiable fact; who they were is not.

There is no public write path. Approving a window is done through
`releases.py` by an operator with database access, the same way Module
17's gate is moved. Building a public approval endpoint here would be
building the authentication this module is explicitly forbidden to build.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from services.public_stats.aggregates import CHARTS
from services.public_stats.config import PublicStatsConfig
from services.public_stats.errors import CHART_NOT_FOUND, PublicStatsError
from services.public_stats.gate import current_scope
from services.public_stats.releases import approved_windows, window_history
from services.public_stats.schemas import (
    ChartResponse,
    Freshness,
    Provenance,
    PublicSummary,
    ReleaseWindowResponse,
)
from services.public_stats.snapshots import read_chart

__all__ = ["create_app"]


def create_app(engine: Engine, config: PublicStatsConfig | None = None) -> FastAPI:
    """Build the public stats app against a database engine."""
    settings = config or PublicStatsConfig()

    app = FastAPI(
        title="ARGUS Public Statistics",
        version="1",
        summary="How ARGUS has actually performed, including every failure.",
        description=(
            "Public and unauthenticated by design. Every figure comes from results a "
            "named human approved for publication — nothing pending or rejected is "
            "reachable through any endpoint here. Statistics below a sample floor show "
            "a count and an explanation rather than a percentage."
        ),
    )
    app.state.engine = engine
    app.state.config = settings

    @app.exception_handler(PublicStatsError)
    async def _error(_request: Request, error: PublicStatsError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content=error.payload())

    app.include_router(_charts_router())
    app.include_router(_releases_router())
    return app


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def get_connection(request: Request) -> Iterator[Connection]:
    engine: Engine = request.app.state.engine
    with engine.begin() as connection:
        yield connection


def get_config(request: Request) -> PublicStatsConfig:
    return request.app.state.config


ConnectionDep = Annotated[Connection, Depends(get_connection)]
ConfigDep = Annotated[PublicStatsConfig, Depends(get_config)]


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------


def _charts_router() -> APIRouter:
    router = APIRouter(prefix="/public/stats", tags=["public-stats"])

    @router.get("", response_model=PublicSummary)
    def summary(connection: ConnectionDep, config: ConfigDep) -> PublicSummary:
        """Is there anything published, and what charts exist.

        One request to answer "does this system have a track record yet".
        `published: false` is not an error — it means nothing has been
        approved, which is currently the honest state.
        """
        scope = current_scope(connection)
        freshness = None
        try:
            stored = read_chart(connection, CHARTS[0], config=config)
            freshness = Freshness(**stored.as_dict()["freshness"])
        except PublicStatsError:
            # Nothing published, or withdrawn. Either way the summary
            # reports the gate state rather than propagating — a caller
            # asking "is there anything" deserves an answer, not a 503.
            freshness = None

        published = not scope.is_empty and freshness is not None
        return PublicSummary(
            published=published,
            charts=list(CHARTS),
            approved_run_count=len(scope.runs),
            approved_window_count=len(scope.windows),
            freshness=freshness,
            explanation=(
                "ARGUS publishes results from validation runs and live-tracked outcome "
                "windows that a named human has approved."
                if published
                else "ARGUS has not published statistics yet: no validation run and no "
                "live release window has been approved. This is not a result of zero — "
                "it is the absence of anything ARGUS is permitted to show."
            ),
        )

    @router.get("/{chart}", response_model=ChartResponse)
    def chart(
        chart: str, connection: ConnectionDep, config: ConfigDep, response: Response
    ) -> ChartResponse:
        """One chart, pre-aggregated and ready to plot."""
        if chart not in CHARTS:
            raise PublicStatsError(
                CHART_NOT_FOUND,
                f"No chart named {chart!r}. Available: {', '.join(CHARTS)}.",
                status=404,
                detail={"chart": chart, "available": list(CHARTS)},
            )

        now = datetime.now(UTC)
        stored = read_chart(connection, chart, config=config, now=now)
        body = stored.as_dict(now)

        # Cache for whatever remains of the refresh cadence, so an
        # intermediary holds something ARGUS considers current rather
        # than guessing a TTL. A stale snapshot gets a short window —
        # it is still served, but a refresh is due.
        remaining = config.settings.refresh_interval.total_seconds() - stored.age_seconds(now)
        response.headers["Cache-Control"] = f"public, max-age={max(int(remaining), 60)}"

        return ChartResponse(
            chart=body["chart"],
            sample_size=body["sample_size"],
            series=body["series"],
            summary=body["summary"],
            caption=body["caption"],
            freshness=Freshness(**body["freshness"]),
            provenance=Provenance(**body["provenance"]),
        )

    return router


# --------------------------------------------------------------------------
# The gate's own decisions, published
# --------------------------------------------------------------------------


def _releases_router() -> APIRouter:
    router = APIRouter(prefix="/public/releases", tags=["public-stats"])

    @router.get("", response_model=list[ReleaseWindowResponse])
    def index(connection: ConnectionDep) -> list[ReleaseWindowResponse]:
        """Every live-outcome window currently approved for publication."""
        return [_window(window) for window in approved_windows(connection)]

    @router.get("/{period_start}/{period_end}", response_model=list[ReleaseWindowResponse])
    def history(
        period_start: str, period_end: str, connection: ConnectionDep
    ) -> list[ReleaseWindowResponse]:
        """Every decision ever made about one window, oldest first.

        Including rejections, and including a rejection that followed an
        approval. A gate that published only its approvals would be a
        record of what ARGUS wanted shown rather than of what happened.
        """
        from datetime import date

        try:
            start, end = date.fromisoformat(period_start), date.fromisoformat(period_end)
        except ValueError as error:
            raise PublicStatsError(
                "INVALID_REQUEST",
                "Period bounds must be ISO dates (YYYY-MM-DD).",
                status=422,
            ) from error
        return [_window(window) for window in window_history(connection, start, end)]

    return router


def _window(window) -> ReleaseWindowResponse:
    return ReleaseWindowResponse(
        period_start=window.period_start.isoformat(),
        period_end=window.period_end.isoformat(),
        data_snapshot_id=str(window.data_snapshot_id),
        sequence_number=window.sequence_number,
        status=window.status.value,
        assigned_at=window.assigned_at,
        by_system=window.by_system,
        note=window.note,
    )
