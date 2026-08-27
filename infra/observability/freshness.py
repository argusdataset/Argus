"""Four-state data freshness, over the PIT columns every canonical table has.

## The four states, and why the fourth is not a worse third

`FRESH` / `DELAYED` / `STALE` / `UNAVAILABLE`. The first three are ages;
the fourth is not an age at all.

- **FRESH** — the newest row arrived inside the fresh window.
- **DELAYED** — older than that, and explicable. A Friday close read on
  Monday is 62 hours old and nothing is wrong.
- **STALE** — old enough that something is wrong, and the number attached
  says how old.
- **UNAVAILABLE** — *there is no row at all*. Not "very old": ARGUS has
  never ingested this feed, or never ingested it for this security. An age
  cannot be computed because there is nothing to compute one from.

Collapsing UNAVAILABLE into STALE is the mistake this file exists to
avoid, and it is the same mistake every module since Module 08 has
refused: `None` is never `0.0`, absence is never a low value. A feed that
has never delivered and a feed that stopped delivering want different
responses from a person — one is a configuration problem, the other is an
outage — and a dashboard showing both as "stale, ∞ hours" makes them
indistinguishable.

## Built on `services/shared/`, not beside it

Module 19 defined `Freshness` — `computed_at`, `as_of`, `age_seconds`,
`stale`, `staleness_reason` — and Modules 20 and 21 serve it. That shape
answers "is this response current" for one response, which is the right
question at an API boundary and the wrong one for a monitor: it is a
boolean where an operator needs a gradient, and it has nowhere to say
"nothing has ever arrived".

So `FeedFreshness` **carries** a `Freshness` rather than replacing it.
`as_freshness()` produces exactly the object Modules 19-21 already serve,
with `stale` set the way they would set it, so a response can embed this
without translation and an operator gets the four-state view from the
same read. One computation, two audiences — rather than a monitor whose
numbers drift from the ones the API is publishing.

## What is measured

`availability_time`, never `event_time`. Module 07's rule, applied to
monitoring: the question a freshness monitor answers is "when did ARGUS
last *learn* something", and a feed delivering last year's bars promptly
is delivering promptly. Measuring `event_time` would report a correct
backfill as an outage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Table, func, select
from sqlalchemy.engine import Connection

from infra.db.schema.canonical import (
    canonical_corporate_actions,
    canonical_fundamentals,
    canonical_ohlcv,
)
from infra.db.schema.news import canonical_news
from infra.observability.config import ObservabilitySettings
from services.shared.schemas import Freshness

__all__ = ["FEEDS", "FeedFreshness", "FreshnessState", "feed_freshness", "all_feeds"]


class FreshnessState(StrEnum):
    """How current a feed is. UNAVAILABLE is not an extreme of the others."""

    FRESH = "FRESH"
    DELAYED = "DELAYED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


#: The canonical feeds worth watching, by name. Every one carries the four
#: PIT columns, so one function answers for all of them — which is the
#: payoff of Module 03 making those columns non-nullable everywhere.
FEEDS: dict[str, Table] = {
    "ohlcv": canonical_ohlcv,
    "fundamentals": canonical_fundamentals,
    "corporate_actions": canonical_corporate_actions,
    "news": canonical_news,
}

_EXPLANATIONS: dict[FreshnessState, str] = {
    FreshnessState.FRESH: "The most recent row arrived inside the fresh window.",
    FreshnessState.DELAYED: (
        "Later than usual but within what a weekend or a holiday explains. Not yet a "
        "problem; a feed that stays here past the next session is."
    ),
    FreshnessState.STALE: (
        "Nothing has arrived for longer than a market closure explains. ARGUS is still "
        "serving what it has, labelled with its age — never as current."
    ),
    FreshnessState.UNAVAILABLE: (
        "No row has ever arrived for this feed. This is not an old value; there is no "
        "value, so there is no age to report. A never-ingested feed and a stopped feed "
        "are different problems."
    ),
}


@dataclass(frozen=True, slots=True)
class FeedFreshness:
    """One feed's currency, as a state and — when there is one — an age."""

    feed: str
    state: FreshnessState
    #: `None` for UNAVAILABLE. Never `0.0`: there is no age, rather than
    #: an age of nothing.
    age_seconds: float | None
    #: When ARGUS last learned something from this feed.
    latest_availability: datetime | None
    #: The instant this was measured at.
    as_of: datetime
    row_count: int
    security_id: UUID | None = None
    explanation: str = ""

    @property
    def healthy(self) -> bool:
        """FRESH and DELAYED are fine. STALE and UNAVAILABLE want a person."""
        return self.state in (FreshnessState.FRESH, FreshnessState.DELAYED)

    def as_freshness(self) -> Freshness:
        """The same fact in the shape Modules 19-21 already serve.

        `stale` is true for STALE and for UNAVAILABLE — from an API
        consumer's side both mean "do not treat this as current", which is
        the only distinction that response shape can carry. The four-state
        view is on this object for the operator who needs the difference.
        """
        return Freshness(
            computed_at=self.latest_availability or self.as_of,
            as_of=self.as_of,
            age_seconds=self.age_seconds if self.age_seconds is not None else 0.0,
            stale=not self.healthy,
            staleness_reason=None if self.healthy else self.explanation,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "feed": self.feed,
            "state": self.state.value,
            "age_seconds": self.age_seconds,
            "latest_availability": (
                self.latest_availability.isoformat() if self.latest_availability else None
            ),
            "as_of": self.as_of.isoformat(),
            "row_count": self.row_count,
            "security_id": str(self.security_id) if self.security_id else None,
            "healthy": self.healthy,
            "explanation": self.explanation,
        }


def feed_freshness(
    connection: Connection,
    feed: str,
    *,
    security_id: UUID | None = None,
    now: datetime | None = None,
    settings: ObservabilitySettings | None = None,
) -> FeedFreshness:
    """How current one canonical feed is, optionally for one security.

    Measured on `availability_time` — when ARGUS learned it — rather than
    `event_time`. A correct backfill of last year's bars is a feed working,
    and measuring the event time would report it as an outage.
    """
    settings = settings or ObservabilitySettings()
    moment = now or datetime.now(UTC)

    table = FEEDS.get(feed)
    if table is None:
        raise KeyError(f"{feed!r} is not a watched feed. The four are {', '.join(sorted(FEEDS))}.")

    query = select(
        func.max(table.c.availability_time).label("latest"),
        func.count().label("rows"),
    )
    if security_id is not None:
        query = query.where(table.c.security_id == security_id)
    row = connection.execute(query).one()

    latest = row.latest
    if latest is None:
        return FeedFreshness(
            feed=feed,
            state=FreshnessState.UNAVAILABLE,
            # None, not 0.0. See the module docstring.
            age_seconds=None,
            latest_availability=None,
            as_of=moment,
            row_count=0,
            security_id=security_id,
            explanation=_EXPLANATIONS[FreshnessState.UNAVAILABLE],
        )

    age = (moment - latest).total_seconds()
    if age <= settings.fresh_within_seconds:
        state = FreshnessState.FRESH
    elif age <= settings.delayed_within_seconds:
        state = FreshnessState.DELAYED
    else:
        state = FreshnessState.STALE

    explanation = _EXPLANATIONS[state]
    if state is FreshnessState.STALE:
        explanation = f"{explanation} Last arrival was {int(age // 3600)} hour(s) ago."

    return FeedFreshness(
        feed=feed,
        state=state,
        age_seconds=age,
        latest_availability=latest,
        as_of=moment,
        row_count=int(row.rows or 0),
        security_id=security_id,
        explanation=explanation,
    )


def all_feeds(
    connection: Connection,
    *,
    now: datetime | None = None,
    settings: ObservabilitySettings | None = None,
) -> list[FeedFreshness]:
    """Every watched feed, in a stable order so two reads compare cleanly."""
    return [feed_freshness(connection, name, now=now, settings=settings) for name in sorted(FEEDS)]
