"""The public contract. Read by strangers, so it explains itself.

Module 19 established three conventions and they are binding here rather
than re-argued — absence explains itself, every response carries its own
as-of, and identity is explicit. `Unavailable`, `Freshness` and
`Provenance` now live in `services/shared/` and are imported from there;
this file defines only what is specific to the public page.

Two things this module adds, both because its reader has no relationship
with ARGUS at all:

## Provenance travels with every number

Every chart response carries which approved runs and which approved live
windows it was computed from. A reader who wants to check ARGUS is not
selecting favourable periods can see the exact set — and, crucially, can
see it change when it changes.

## Insufficiency is louder here than anywhere else

Internally a thin sample yields `None` and an analyst knows what that
means. Publicly it yields a count, a required minimum, and a sentence
explaining why no percentage is shown. The sentence is deliberately
unflattering: a page that hid its thin buckets would look better and
would be the thing this whole module exists not to be.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Defined once in `services/shared/` — see that package on why the three
# response blocks are shared and the error codes are not.
from services.shared.schemas import Freshness, Provenance, Unavailable

__all__ = [
    "ChartResponse",
    "StatsProvenance",
    "Freshness",
    "Provenance",
    "PublicSummary",
    "ReleaseWindowResponse",
    "Unavailable",
]


class StatsProvenance(Provenance):
    """Exactly which approved results produced these numbers.

    Published so a reader can check ARGUS is not quietly choosing which
    periods to count. If this set changes, the numbers changed with it.

    Subclasses the shared base rather than replacing it: `note` and the
    convention that provenance travels with every number are shared;
    these two identifier lists are this service's own.
    """

    approved_runs: list[str] = Field(
        default_factory=list, description="Validation runs a named human approved."
    )
    approved_live_windows: list[dict[str, str]] = Field(
        default_factory=list,
        description="Windows of live-tracked outcomes a named human approved.",
    )


class ChartResponse(BaseModel):
    """One chart, ready to plot, with everything needed to judge it.

    `series` and `summary` are deliberately loosely typed: each of the
    four charts has a different row shape, and forcing them into one model
    would either lose information or produce a union nobody can read. The
    shapes are stable and documented per chart in `aggregates.py`.
    """

    chart: str
    sample_size: int
    series: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    caption: str = Field(
        description="A plain-language sentence about what this chart shows and does not."
    )
    freshness: Freshness
    provenance: StatsProvenance


class PublicSummary(BaseModel):
    """What ARGUS is currently publishing at all, before any chart.

    Exists so a consumer can answer "is there anything here" in one
    request. `published` false is not an error and not a zero — it means
    no validation run and no live window has been approved, so ARGUS has
    nothing it is permitted to show.
    """

    published: bool
    charts: list[str]
    approved_run_count: int
    approved_window_count: int
    freshness: Freshness | None = None
    explanation: str


class ReleaseWindowResponse(BaseModel):
    """One decision about publishing a window of live-tracked outcomes.

    Public, deliberately. The review gate is part of ARGUS's claim to be
    checkable, and a gate whose decisions were private would be a claim
    nobody could verify.
    """

    period_start: str
    period_end: str
    data_snapshot_id: str
    sequence_number: int
    status: str
    assigned_at: datetime
    #: Whether a person or the system made this assignment. The reviewer's
    #: identity is deliberately not published — that a named human decided
    #: is the checkable fact; who they were is not the public's business.
    by_system: bool
    note: str | None = None
