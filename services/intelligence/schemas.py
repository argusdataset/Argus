"""The Intelligence API's own shapes. Explicitly not Module 19's.

## Why these are not Module 19's watchlist schemas

Module 19 flagged this for this module, and it is worth restating because
the two things share a word and nothing else:

- A **User Watchlist** is a person's list. They made it, named it, put
  things on it. Its entry is `(security, when they added it, where they
  put it)`.
- An **ARGUS Intelligence Watchlist** is a *derived view* — a live query
  over `market_state`. Nobody made it, nothing is stored, and its entry
  is `(security, the state it is in, when it entered that state, what
  ARGUS scores it)`.

Giving them one shape would mean a client that could not tell a list
somebody curated from a list ARGUS derived, and a field like `position`
would have to mean "where the user dragged it" in one and nothing in the
other. So: separate models, no import from `services.terminal.schemas`
for anything watchlist-shaped, and an AST test that keeps it that way.

## Score numbers, never fundamentals

Stated early in the project and enforced structurally here. An
Intelligence entry shows `argus_score`, `confidence`, `opportunity_score`,
`risk_score` and `probability` — what ARGUS thinks. Revenue and P/E belong
to the Terminal, which is a different question a person asks about the
same company.

## `Unavailable` is the common case, not the edge

Most candidates today produce `INSUFFICIENT_EVIDENCE`, and that is correct
behaviour rather than a degraded one. So `ScoreBlock` carries either the
five numbers or an `Unavailable` naming the decision — and the second is
a complete answer with Module 16's narration attached, not a stub.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# The three shared response blocks. See `services/shared/` on why these
# are shared and the error codes are not.
from services.shared.schemas import Freshness, Provenance, Unavailable

__all__ = [
    "CaseExplanation",
    "ExplanationBlock",
    "Freshness",
    "IntelligenceEntry",
    "IntelligenceProvenance",
    "IntelligenceWatchlist",
    "OverlayResponse",
    "RiskBlock",
    "ScoreBlock",
    "SecurityDetail",
    "SimilarityBlock",
    "SimilarityScope",
    "StateBlock",
    "Unavailable",
]


class IntelligenceProvenance(Provenance):
    """The configuration versions behind a score, so it can be reproduced.

    Module 03 made all six NOT NULL on `signals` for exactly this reason:
    a score whose configuration cannot be recovered is a number nobody can
    ever check. Publishing them means a reader can.
    """

    target_model_version_id: str | None = None
    scoring_configuration_id: str | None = None
    feature_schema_version_id: str | None = None
    data_snapshot_id: str | None = None
    universe_version_id: str | None = None
    detection_configuration_id: str | None = None
    signal_id: str | None = None


class ScoreBlock(BaseModel):
    """ARGUS's five numbers, or an honest account of why there are none.

    `scored` is the discriminator a client branches on. When false, every
    number is absent and `unavailable` says which decision produced that —
    `INSUFFICIENT_EVIDENCE`, `GATED_INELIGIBLE`, `GATED_LOST_ELIGIBILITY`.
    Those are different situations and a client showing different copy for
    each is doing the right thing.
    """

    scored: bool
    evidence_status: str | None = None
    argus_score: float | None = None
    confidence: float | None = None
    opportunity_score: float | None = None
    risk_score: float | None = None
    #: Always None today. Module 13 stores no probability, deliberately —
    #: a calibrated model does not exist and a plausible placeholder here
    #: would be the single most damaging number in the system.
    probability: float | None = None
    probability_status: str | None = None
    #: The seven weighted components, by name, each with what it measured
    #: and what it could not.
    components: dict[str, Any] = Field(default_factory=dict)
    weight_coverage: float | None = None
    unavailable: Unavailable | None = None
    provenance: IntelligenceProvenance | None = None
    computed_at: datetime | None = None


class PhaseTransition(BaseModel):
    """One recorded state change, from Module 10's append-only log.

    Not a computation — every field is read straight off
    `market_state_transitions`, which is the authority; nothing here
    re-derives or interprets it. `duration_in_prior_state_seconds` is
    `None` only for a security's first-ever recorded transition, the same
    case Module 10's own `duration_in_prior_state` leaves `NULL`.
    """

    from_state: str | None = None
    to_state: str
    transition_time: datetime
    duration_in_prior_state_seconds: float | None = None
    confidence: float | None = None


class IntelligenceEntry(BaseModel):
    """One security on a derived watchlist.

    Carries `security_id` alongside the ticker for the reason every ARGUS
    response does: tickers are recycled, identity is not.

    `phase_history` and `mfe` are populated only on the `UPTREND`
    (confirmed moves) watchlist — the one list where showing lineage is
    the point. On the other three they are `None`, not `[]`: an empty list
    would claim "no history exists", where `None` correctly says "this
    list does not show it". `mfe` is `None` whenever the security's setup
    has not yet concluded, which is the common case for a security still
    actively in `UPTREND` — Module 15 only measures a maximum favourable
    excursion once a setup's outcome is recorded, and this entry reports
    that absence rather than computing a live figure nothing upstream has
    validated.
    """

    security_id: UUID
    ticker: str | None = None
    name: str | None = None
    #: Module 10's current state, and when it entered. Read from the
    #: projection, never re-derived here.
    state: str
    entered_at: datetime
    state_confidence: float | None = None
    score: ScoreBlock
    phase_history: list[PhaseTransition] | None = None
    mfe: float | None = None


class IntelligenceWatchlist(BaseModel):
    """One of the four derived watchlists, as it is right now.

    `stored: false` is on the wire on purpose. This list is a query over
    `market_state`, not a table — a client caching it should know it is
    caching a view of something that moves, and a client comparing it to a
    User Watchlist should be able to see they are different kinds of
    thing.
    """

    name: str
    states: list[str] = Field(
        description="The market states this list covers. Module 10 defines the mapping."
    )
    entries: list[IntelligenceEntry] = Field(default_factory=list)
    count: int
    stored: bool = Field(
        default=False,
        description=(
            "Always false. This list is a live query over market_state, never a stored "
            "table — there is no second place membership could be recorded."
        ),
    )
    freshness: Freshness


class SimilarityScope(BaseModel):
    """One scope's historical evidence, with its sufficiency intact.

    Cross-asset and same-asset are separate objects and are never blended.
    Module 11 refused to combine them — "this security did this before"
    and "similar securities did this before" are different kinds of
    evidence — and combining them at the last step would undo that.
    """

    scope: str
    match_count: int
    sufficiency: str
    median_outcome: float | None = None
    average_outcome: float | None = None
    failure_rate: float | None = None
    failure_rate_interval: dict[str, float] | None = None
    outcome_by_regime: dict[str, Any] | None = None
    unavailable: Unavailable | None = None


class SimilarityBlock(BaseModel):
    """Both scopes, side by side, never summed."""

    cross_asset: SimilarityScope | None = None
    same_asset: SimilarityScope | None = None
    unavailable: Unavailable | None = None
    computed_at: datetime | None = None


class RiskBlock(BaseModel):
    """Itemized risk inputs, including the honestly undetermined ones.

    Module 12's rule, carried to the wire: a flag ARGUS could not evaluate
    is reported as undetermined, never as "no risk". The two look
    identical to a careless consumer and mean opposite things.
    """

    flags: dict[str, Any] = Field(default_factory=dict)
    undetermined: list[str] = Field(default_factory=list)
    pending_events: list[dict[str, Any]] = Field(default_factory=list)
    unavailable: Unavailable | None = None


class StateBlock(BaseModel):
    """Module 10's current classification and the evidence behind it."""

    state: str
    entered_at: datetime
    confidence: float | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    watchlists: list[str] = Field(
        default_factory=list,
        description="Which of the four derived lists this state puts the security on.",
    )


class ExplanationBlock(BaseModel):
    """Module 16's output, passed through unchanged.

    Read from `Explanation.as_dict()` and not touched. This module does not
    generate, paraphrase or summarise explanation text — Module 16 built a
    fabrication guard around that text, and rewriting it here would step
    outside the guard.

    The field shapes are Module 16's, not this module's, and that is why a
    headline is an object rather than a string: every claim carries the
    fact keys it rests on, and flattening it to a sentence here would drop
    exactly the part that makes the sentence checkable.

    `facts` is the registry the explanation was built from, published for
    the same reason Module 16 attaches it: a consumer that wants to verify
    a sentence, or link a number back to the field it came from, can do it
    without asking anything else.
    """

    kind: str
    subject: str
    #: `{"text": ..., "cites": [...]}` — Module 16's `Claim`.
    headline: dict[str, Any] = Field(default_factory=dict)
    #: The whole narrative as one string, produced by Module 16's own
    #: `text()`. Convenience for a caller rendering a paragraph; the
    #: sections are what a caller rendering structure should read.
    text: str = ""
    sections: list[dict[str, Any]] = Field(default_factory=list)
    #: Dimensions Module 16 deliberately left out, named. An omitted input
    #: silently dropped would be indistinguishable from one that was fine.
    omissions: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    unavailable: Unavailable | None = None


class SecurityDetail(BaseModel):
    """Everything ARGUS knows about one security right now, assembled.

    Assembled, not computed. Every block is a stored artefact from an
    earlier module, reshaped for a consumer and otherwise untouched.
    """

    security_id: UUID
    ticker: str | None = None
    name: str | None = None
    state: StateBlock | None = None
    score: ScoreBlock
    similarity: SimilarityBlock
    risk: RiskBlock
    explanation: ExplanationBlock
    freshness: Freshness


class OverlayResponse(BaseModel):
    """ARGUS-specific chart overlays. Bars come from the Terminal's datafeed.

    This response deliberately contains **no OHLCV**. Module 19 serves
    bars; duplicating that here would create a second price series that
    could disagree with the first. `bars_endpoint` names where to get
    them.
    """

    security_id: UUID
    ticker: str | None = None
    bars_endpoint: str = Field(
        description="Module 19's datafeed. This service does not serve price bars."
    )
    #: TradingView-shaped marks, one per state transition.
    marks: list[dict[str, Any]] = Field(default_factory=list)
    #: Time spans during which the security was consolidating. A time
    #: band, not a price band — see `overlays.py` on why the price
    #: boundaries are unavailable. A span with no recorded exit carries
    #: `end: null` and `ongoing: true`.
    consolidation_zones: list[dict[str, Any]] = Field(default_factory=list)
    zone_price_boundaries: Unavailable | None = None
    freshness: Freshness


class CaseExplanation(BaseModel):
    """Why a concluded setup ended the way it did.

    Module 15's case record narrated by Module 16's `explain_case`, passed
    through. Successes and failures take the identical path — Module 15
    built its records so a failure is as complete as a success, and
    narrating failures more thinly would undo that at the last step.
    """

    setup_id: UUID
    security_id: UUID
    outcome_status: str
    explanation: ExplanationBlock
    #: Module 15's classification, including its own stated limits.
    false_positive_type: str | None = None
    false_positive_confidence: str | None = None
    review_confidence: str | None = None
    #: Why the false-positive label is only as good as it is. Module 15
    #: called these thresholds unvalidated; saying so beside the label is
    #: the difference between a classification and a claim.
    classification_caveat: str = ""
    freshness: Freshness
