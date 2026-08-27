"""The three response blocks every ARGUS service needs, defined once.

Each answers a question that is identical in every service, and three
different answers to the same question is the drift this package exists
to prevent.

## `Unavailable` — why is this missing

Eighteen modules have enforced one rule harder than any other: `None` is
never `0.0`, and an absence is a different fact from a measurement. That
rule dies on contact with JSON, where `null` means everything and
nothing.

So a block that could not be produced is not `null`. It is present,
marked `available: false`, and carries a `reason` naming which case
applied. A consumer rendering "—" versus "no data yet" versus "not enough
evidence" can tell them apart without guessing.

This matters most in Module 21, where `INSUFFICIENT_EVIDENCE` is not an
edge case but the majority outcome — most candidates genuinely cannot be
scored yet, and saying so precisely is more useful than a number would
have been.

## `Freshness` — how old is this

Every point-in-time answer carries the instant it was true as of, not the
time the request was served. Two calls naming the same cutoff return the
same data, which is what makes a screenshot reproducible.

`stale` is reported, never enforced. A day-old true number beats a
spinner; the reader decides whether the age matters for what they are
asking.

## `Provenance` — where did this come from

Which stored artefacts produced these numbers. Module 20 publishes the
approved runs behind a public statistic so a stranger can check ARGUS is
not choosing favourable periods; Module 21 publishes the configuration
versions behind a score so it can be reproduced. Same question, same
block.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

__all__ = ["Freshness", "Provenance", "Unavailable"]


class Unavailable(BaseModel):
    """Why a block of data could not be produced.

    Never `null` in its place. A consumer that sees this knows the request
    succeeded and ARGUS genuinely has nothing, and knows which kind of
    nothing.
    """

    available: Literal[False] = False
    #: A stable machine-readable token — a `MissReason`, a sufficiency
    #: state, a scoring decision. Consumers branch on this.
    reason: str
    #: Prose for a log or a tooltip. Never parsed.
    explanation: str
    #: Optional counts, for the sample-floor case: how much evidence there
    #: is against how much is required.
    observed: int | None = None
    required: int | None = None


class Freshness(BaseModel):
    """When these figures were true, and whether that is recent enough."""

    computed_at: datetime = Field(description="When ARGUS produced this answer.")
    as_of: datetime = Field(
        description=(
            "The point-in-time cutoff. Everything here was knowable to ARGUS at this "
            "instant; nothing recorded later is visible."
        )
    )
    age_seconds: float
    stale: bool
    staleness_reason: str | None = None


class Provenance(BaseModel):
    """Which stored artefacts produced these numbers — the base shape.

    Only `note` is fixed here, and that is deliberate rather than thin.
    What provenance *means* is identical everywhere — "here is what this
    came from, in prose and in identifiers" — but the identifiers are not:
    a public statistic cites approved validation runs, a score cites
    configuration versions, and a watchlist entry cites the state
    transition behind it.

    So services subclass this and add their own fields. Flattening all
    three into one `sources: dict` would have shared more and said less,
    and it would have changed Module 20's already-published response
    shape, which the extraction was explicitly not supposed to do.
    """

    note: str = Field(
        default="",
        description="What the identifiers in this block mean, in plain language.",
    )
