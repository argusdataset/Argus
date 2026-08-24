"""News for the Terminal. Never scored, and the module says so twice.

## The boundary this file sits on

The project has been explicit since early planning: fundamentals and news
are a **context layer**, never a `target-model-v1` or `argus_score` input.
Module 13 keeps `fundamental_context` at 0% weight for exactly this
reason. So this module reads news and returns it, and imports nothing
from `core/scoring/` or `core/market_state/` — a structural test asserts
that, because a boundary that is only a convention is a boundary that
eventually gets crossed by someone in a hurry.

## Point-in-time, with a simpler rule than fundamentals

An article is not restated. A correction is a new article and the
original stays as published — so there is no "latest revision of this
fact" query here, only the ordinary `availability_time <= as_of` filter
every canonical table carries. That filter is what stops a Terminal view
of last March showing an article published in June.

## Two kinds of empty

`ever_ingested` distinguishes "ARGUS has never held news for this
security" from "it has news, none of it knowable by this cutoff". Module
18's report made the same distinction binding for scan availability, and
the reasoning transfers exactly: an empty list is two different facts, and
a consumer rendering "no coverage" versus "nothing recent" needs to know
which.

Today the honest answer is almost always the first: no news has been
ingested, because the FMP subscription is lapsed and Module 04's news
fetcher has never run against a live key. The endpoint reports that
plainly rather than looking broken.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.engine import Connection

from infra.db.schema.news import canonical_news
from services.terminal.company import resolve_company
from services.terminal.config import TerminalConfig
from services.terminal.schemas import NewsArticle, NewsResponse

__all__ = ["read_news"]


def read_news(
    connection: Connection,
    ticker: str,
    *,
    as_of: datetime | None = None,
    limit: int | None = None,
    config: TerminalConfig | None = None,
) -> NewsResponse:
    """This security's articles, newest first, bounded by availability."""
    config = config or TerminalConfig()
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)
    count = config.limits.bounded_news_limit(limit)

    rows = connection.execute(
        select(
            canonical_news.c.headline,
            canonical_news.c.event_time,
            canonical_news.c.source_site,
            canonical_news.c.url,
            canonical_news.c.summary,
        )
        .where(
            canonical_news.c.security_id == profile.security_id,
            canonical_news.c.availability_time <= moment,
        )
        .order_by(desc(canonical_news.c.event_time), desc(canonical_news.c.id))
        .limit(count)
    ).all()

    return NewsResponse(
        security=profile,
        as_of=moment,
        articles=[
            NewsArticle(
                headline=row.headline,
                published_at=row.event_time,
                source_site=row.source_site,
                url=row.url,
                summary=row.summary,
            )
            for row in rows
        ],
        # Asked without the availability filter on purpose: this is the
        # question "does ARGUS carry news for this name at all", which is
        # not the same question as "is any of it visible right now".
        ever_ingested=_ever_ingested(connection, profile.security_id),
    )


def _ever_ingested(connection: Connection, security_id: UUID) -> bool:
    return bool(
        connection.execute(
            select(func.count())
            .select_from(canonical_news)
            .where(canonical_news.c.security_id == security_id)
            .limit(1)
        ).scalar_one()
    )
