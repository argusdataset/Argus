"""Reading ARGUS's published track record from the service that owns it.

## One source of truth, two surfaces

`services/public_stats/aggregates.py` decides what ARGUS is allowed to
say about its own performance — which runs are approved, what the sample
floor is, when a rate may be quoted at all. This module does not import
any of it. It calls the same HTTP API the public website calls, so the
bot and the page cannot disagree.

Recomputing here would be a second implementation of a claim about
ARGUS's honesty, and the failure mode is the worst kind: both numbers
look plausible, they differ, and nobody can say which is wrong.

## Over the private network, not the public URL

Both services run in the same Railway project, so the bot reaches
`public_stats` on Railway's internal DNS
(`<service>.railway.internal`) rather than going out to the edge and
back. That is one hop instead of three, it does not consume the public
service's edge bandwidth, and it does not depend on the public domain
existing.

**The one thing that makes it work, and would silently not.** In
production `public_stats` runs behind `TlsPolicyMiddleware` with
`require_https=True`, which refuses a plaintext request — and the
internal hop *is* plaintext, because Railway terminates TLS at its edge
and the private network carries no certificate. Without help, every
internal call would come back as a 308 to an `https://` URL nothing is
listening on.

So the client sends `X-Forwarded-Proto: https`, which is exactly what
that header is for: the peer is inside Railway's private network — a
range `RAILWAY_PRIVATE_NETWORK` already declares trusted, and one not
routable from the internet — and the header states the transport ARGUS
was reached over. `tls.py`'s `request_scheme` believes it only from a
trusted peer, which is what keeps this from being a bypass anybody
outside could use.

`tests/integration/telegram/test_statistics.py` runs this client against
a real production-profile `public_stats` app and asserts the call
succeeds, because "works in development, 308s in production" is exactly
the shape of the bug this project has already hit once.

## Three states, kept three

`public_stats` distinguishes three answers and this module refuses to
collapse them:

1. **Published, with numbers** — a rate ARGUS is permitted to quote.
2. **Published, but too thin** — the chart exists and says
   "N of M required". Not a zero, and not an error.
3. **Nothing published at all** — the review gate has approved nothing.
   `published: false`, which is the honest current state.

And a fourth this module adds, which is about the bot rather than about
ARGUS: **unreachable**, when `public_stats` cannot be called. A reader
must not be told "no track record yet" because a container was
restarting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from infra.observability.logging import get_logger

__all__ = [
    "FORWARDED_PROTO_HEADER",
    "ChartSummary",
    "ReleaseSummary",
    "StatsClient",
    "StatsSnapshot",
    "StatsState",
    "V1_CHARTS",
    "read_statistics",
]

_log = get_logger("argus.telegram.stats")

#: See the module docstring. Sent on every internal request.
FORWARDED_PROTO_HEADER = "X-Forwarded-Proto"

#: The charts this version summarises, in the order they are shown.
#:
#: `regime_breakdown`, `excursion_distribution` and `top_performers` are
#: deliberately absent: each is a *distribution*, and a distribution
#: flattened into three text lines is the kind of summary that misleads
#: by omission. They belong on the page, which has room to draw them, and
#: the message links there.
V1_CHARTS: tuple[str, ...] = ("win_rate", "cumulative_performance")


class StatsState(StrEnum):
    """Which of the four answers this reader got."""

    #: The gate has approved something and the charts exist.
    PUBLISHED = "published"
    #: Nothing has been approved. Not an error, not a zero.
    NOT_PUBLISHED = "not_published"
    #: `public_stats` could not be reached or did not answer usefully.
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class ChartSummary:
    """One chart, reduced to what a text message can carry honestly."""

    chart: str
    sample_size: int
    #: False when the chart exists but the sample is under the floor.
    available: bool
    values: dict[str, Any] = field(default_factory=dict)
    observed: int | None = None
    required: int | None = None
    caption: str = ""


@dataclass(frozen=True, slots=True)
class ReleaseSummary:
    """Whether the review gate has decided anything, and what most recently."""

    count: int
    latest_status: str | None = None
    latest_period_end: str | None = None


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    """Everything one Statistics request found."""

    state: StatsState
    explanation: str = ""
    charts: tuple[ChartSummary, ...] = ()
    releases: ReleaseSummary | None = None
    #: Why it was unreachable. Never shown verbatim to a user — the copy
    #: says "cannot be reached", and this is for the log.
    detail: str = ""

    @property
    def published(self) -> bool:
        return self.state is StatsState.PUBLISHED


class StatsClient:
    """Calls `public_stats` over the private network. Never raises."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={FORWARDED_PROTO_HEADER: "https"},
        )

    def __enter__(self) -> StatsClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, path: str) -> tuple[int, Any]:
        """One GET. Returns `(status, body)`, or `(0, None)` if it failed.

        Status `0` rather than an exception because every caller here
        wants to distinguish "the service said no" from "the service was
        not there", and a raised exception makes the second look like a
        bug in the bot.
        """
        try:
            response = self._client.get(path)
        except httpx.HTTPError as error:
            _log.warning(
                "public_stats unreachable",
                extra={
                    "event": "public_stats_unreachable",
                    "path": path,
                    "error_type": type(error).__name__,
                },
            )
            return 0, None

        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None


def read_statistics(client: StatsClient, *, charts: tuple[str, ...] = V1_CHARTS) -> StatsSnapshot:
    """One Statistics view: the summary, the v1 charts, and the gate's decisions."""
    status, summary = client.get("/public/stats")

    # Anything but a 200 is "unreachable", including a 503 from the gate
    # itself. That is deliberate and was a real bug for one commit: a 503
    # carries a JSON error body, so a check that only looked at the shape
    # of the body found no `published` key and reported "nothing has been
    # approved" — turning a service fault into a false statement about
    # ARGUS's track record, which is the one thing this menu must never
    # do.
    if status != httpx.codes.OK or not isinstance(summary, dict):
        return StatsSnapshot(
            state=StatsState.UNREACHABLE,
            detail=f"summary status={status}",
        )

    if not summary.get("published"):
        # The gate has approved nothing. `explanation` is `public_stats`'s
        # own words, quoted rather than paraphrased so the bot and the
        # page say the same thing.
        return StatsSnapshot(
            state=StatsState.NOT_PUBLISHED,
            explanation=str(summary.get("explanation") or ""),
            releases=_releases(client),
        )

    return StatsSnapshot(
        state=StatsState.PUBLISHED,
        explanation=str(summary.get("explanation") or ""),
        charts=tuple(
            found for found in (_chart(client, name) for name in charts) if found is not None
        ),
        releases=_releases(client),
    )


def _chart(client: StatsClient, name: str) -> ChartSummary | None:
    """One chart, or None if this one alone could not be read.

    None rather than propagating: a snapshot that has the win rate and
    could not get the cumulative curve should show the win rate. The
    absent one simply does not appear, which is what the sample-size line
    already teaches a reader to expect.
    """
    status, body = client.get(f"/public/stats/{name}")
    if status != httpx.codes.OK or not isinstance(body, dict):
        return None

    summary = body.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    unavailable = summary.get("unavailable")
    unavailable = unavailable if isinstance(unavailable, dict) else None

    return ChartSummary(
        chart=str(body.get("chart") or name),
        sample_size=int(body.get("sample_size") or 0),
        available=unavailable is None,
        values=summary,
        observed=_int(unavailable, "observed"),
        required=_int(unavailable, "required"),
        caption=str(body.get("caption") or ""),
    )


def _releases(client: StatsClient) -> ReleaseSummary | None:
    """How many windows the gate has decided on, and the most recent one.

    Count and latest decision only — the full table is the page's job.
    """
    status, body = client.get("/public/releases")
    if status != httpx.codes.OK or not isinstance(body, list):
        return None

    windows = [entry for entry in body if isinstance(entry, dict)]
    if not windows:
        return ReleaseSummary(count=0)

    # `sequence_number` is Module 20's monotonic order, which exists
    # because "latest by timestamp" over rows written in one transaction
    # is a coin flip — the hazard Module 23 catalogued four instances of.
    latest = max(windows, key=lambda entry: int(entry.get("sequence_number") or 0))
    return ReleaseSummary(
        count=len(windows),
        latest_status=str(latest.get("status") or "") or None,
        latest_period_end=str(latest.get("period_end") or "") or None,
    )


def _int(source: dict[str, Any] | None, key: str) -> int | None:
    if source is None:
        return None
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)
