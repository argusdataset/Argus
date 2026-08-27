"""The two anomalies earlier modules asked for by name.

Neither is invented here. Module 10 built the distinction one of them
counts; Module 17 wrote the sentence that describes the other. This file
does the counting, which is the step both were missing.

## `no_state_predicate_matched` — a hole in the state machine

Module 10 split UNCLASSIFIED into two reasons and said why:

> "`insufficient_features_for_any_state` — the evidence was too thin to
> judge... `no_state_predicate_matched` — the evidence was fine and no
> state claimed the security. That is a **gap in the state machine**, not
> a data problem, and it is invisible unless named. Counting these is how
> a modeling hole gets found."

Module 10 named it and wrote it into `market_state_transitions.evidence`.
Nothing counted it. `state_gaps` does, and reads that stored evidence
rather than re-running the classifier — re-running it would produce
today's answer about a transition recorded last month, which is a
different question wearing the same name.

**One honest limitation, stated because it changes what the number
means.** The transition log records a security *entering* UNCLASSIFIED. A
security that sits there for a month writes one row, not thirty. So this
counts distinct occurrences of the gap, not security-days spent in it —
which is the right number for "is the state machine missing a case" and
the wrong one for "how much of the universe is unclassified right now".
The second question is answered from the `market_state` projection, and
`unclassified_now` does that separately rather than conflating them.

## The explanation revert rate

Module 17 flagged it precisely:

> "an explanation should never fabricate, but... a quietly high revert
> rate would look like success. Logging the revert count per run would
> make that visible."

The failure mode is specific and nasty: `VerifiedRenderer` reverts any
rephrasing that does not verify, so a renderer producing nothing usable
yields perfectly correct output. Every explanation verifies. The system
looks healthy while the model contributes nothing — or worse, while it
tries repeatedly to introduce claims the verifier catches.

**This is observed without touching Module 16.** `VerifiedRenderer` keeps
no tally and this module does not add one to it. Instead
`observed_renderer` slips a recorder *between* the verifier and the
renderer it wraps: the recorder sees what was proposed, the final
explanation shows what survived, and the verdict per claim is read off
those two. Kept means the proposal is in the output; reverted means it is
not. Module 16's `verify` is never reimplemented — its decision is
observed, not recomputed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Connection

from core.explanation.narrative import Explanation
from core.explanation.renderers import Renderer, VerifiedRenderer
from infra.db.enums import MarketState
from infra.db.schema.intelligence import market_state, market_state_transitions
from infra.observability.config import ObservabilitySettings

__all__ = [
    "NO_PREDICATE_MATCHED",
    "THIN_EVIDENCE",
    "ProposalRecorder",
    "RenderTally",
    "StateGapReport",
    "observed_renderer",
    "state_gaps",
    "unclassified_now",
]

#: Module 10's own strings. Referenced, never restated as literals at a
#: call site, so a rename upstream breaks the import rather than silently
#: making this monitor count zero forever.
NO_PREDICATE_MATCHED = "no_state_predicate_matched"
THIN_EVIDENCE = "insufficient_features_for_any_state"

_REASON = market_state_transitions.c.evidence["unclassified_reason"].astext


# --------------------------------------------------------------------------
# Market state: the gap in the state machine
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateGapReport:
    """How often the state machine had evidence and no state to give.

    `gap_occurrences` is the number that matters. `thin_evidence_
    occurrences` sits beside it because the ratio is what makes the first
    number readable: ten gaps against ten thousand thin-data cases is
    noise, ten against twelve is a state machine that mostly does not
    work.
    """

    as_of: datetime
    since: datetime
    gap_occurrences: int
    thin_evidence_occurrences: int
    securities: list[str] = field(default_factory=list)
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    @property
    def gap_share(self) -> float | None:
        """Gaps as a share of all UNCLASSIFIED entries. `None` when there are none."""
        total = self.gap_occurrences + self.thin_evidence_occurrences
        return None if total == 0 else self.gap_occurrences / total

    @property
    def healthy(self) -> bool:
        """Any occurrence at all is worth a look.

        No threshold, deliberately. Module 10 called this "a gap in the
        state machine" — a modelling hole, not a rate to tolerate — and a
        threshold here would be this module inventing an acceptable amount
        of a thing the module that produces it considers a defect.
        """
        return self.gap_occurrences == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "since": self.since.isoformat(),
            "healthy": self.healthy,
            "gap_occurrences": self.gap_occurrences,
            "thin_evidence_occurrences": self.thin_evidence_occurrences,
            "gap_share": self.gap_share,
            "securities": self.securities,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }


def state_gaps(
    connection: Connection,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
    settings: ObservabilitySettings | None = None,
) -> StateGapReport:
    """Occurrences of `no_state_predicate_matched`, from Module 10's evidence."""
    settings = settings or ObservabilitySettings()
    moment = now or datetime.now(UTC)
    window_start = since or moment - timedelta(days=float(settings.anomaly_window_days))

    counted = connection.execute(
        select(_REASON.label("reason"), func.count().label("count"))
        .where(
            and_(
                market_state_transitions.c.transition_time >= window_start,
                market_state_transitions.c.transition_time <= moment,
                _REASON.is_not(None),
            )
        )
        .group_by(_REASON)
    ).all()
    tally = {row.reason: int(row.count) for row in counted}

    detail = connection.execute(
        select(
            market_state_transitions.c.security_id,
            func.min(market_state_transitions.c.transition_time).label("first_seen"),
            func.max(market_state_transitions.c.transition_time).label("last_seen"),
        )
        .where(
            and_(
                market_state_transitions.c.transition_time >= window_start,
                market_state_transitions.c.transition_time <= moment,
                _REASON == NO_PREDICATE_MATCHED,
            )
        )
        .group_by(market_state_transitions.c.security_id)
        .order_by(func.max(market_state_transitions.c.transition_time).desc())
        .limit(int(settings.max_rows_sampled))
    ).all()

    return StateGapReport(
        as_of=moment,
        since=window_start,
        gap_occurrences=tally.get(NO_PREDICATE_MATCHED, 0),
        thin_evidence_occurrences=tally.get(THIN_EVIDENCE, 0),
        securities=[str(row.security_id) for row in detail],
        first_seen=min((row.first_seen for row in detail), default=None),
        last_seen=max((row.last_seen for row in detail), default=None),
    )


def unclassified_now(connection: Connection) -> int:
    """How many securities sit UNCLASSIFIED in the projection right now.

    Separate from `state_gaps` on purpose. That counts entries into the
    state over a window; this counts occupancy at an instant. Reporting
    one as the other is the mistake — a single security stuck for a month
    is one occurrence and thirty days of occupancy, and the two numbers
    answer different questions.
    """
    return int(
        connection.execute(
            select(func.count())
            .select_from(market_state)
            .where(market_state.c.state == MarketState.UNCLASSIFIED.value)
        ).scalar_one()
    )


# --------------------------------------------------------------------------
# Explanation: the revert rate
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RenderTally:
    """What a renderer proposed and what the verifier let through.

    Mutable on purpose: it accumulates across a run, which is the unit
    Module 17 asked for ("the revert count per run").
    """

    explanations: int = 0
    claims: int = 0
    proposed: int = 0
    kept: int = 0
    reverted: int = 0

    @property
    def revert_rate(self) -> float | None:
        """Reverted as a share of proposed. `None` when nothing was proposed.

        `None` rather than `0.0`, for the reason every module since 08 has
        given: a renderer that proposed nothing has no revert rate, and
        reporting it as a perfect zero would make silence look like
        success — which is the exact failure Module 17 flagged.
        """
        return None if self.proposed == 0 else self.reverted / self.proposed

    @property
    def healthy(self) -> bool:
        """Nothing proposed is healthy; everything reverted is not.

        A renderer that never proposes is the deterministic one doing its
        job. A renderer that proposes and has all of it reverted is
        contributing nothing while looking perfect, and that is the case
        worth surfacing.
        """
        return self.proposed == 0 or self.reverted < self.proposed

    def as_dict(self) -> dict[str, Any]:
        return {
            "explanations": self.explanations,
            "claims": self.claims,
            "proposed": self.proposed,
            "kept": self.kept,
            "reverted": self.reverted,
            "revert_rate": self.revert_rate,
            "healthy": self.healthy,
        }


@dataclass(slots=True)
class ProposalRecorder:
    """A renderer that records what the one it wraps proposed, then passes it on.

    Sits between `VerifiedRenderer` and the real renderer. It changes
    nothing — `render` returns exactly what the inner renderer returned —
    and it is the only way to see the proposals without reimplementing
    Module 16's verification to work out which of them survived.
    """

    inner: Renderer
    tally: RenderTally
    name: str = "recorder"
    #: The last proposal set, positionally. Read by `_ObservedVerifier`.
    last: tuple[str, ...] = ()

    def render(self, explanation: Explanation) -> Explanation:
        rendered = self.inner.render(explanation)
        self.last = tuple(claim.text for claim in _claims(rendered))
        return rendered


@dataclass(slots=True)
class _ObservedVerifier:
    """`VerifiedRenderer` with the verdicts read off its own output.

    Wraps rather than modifies. The original claims are the input, the
    proposals come from the recorder, and the final text says which
    survived — so the verifier's decision is observed rather than
    recomputed, and Module 16's `verify` is never called from here.
    """

    verifier: VerifiedRenderer
    recorder: ProposalRecorder
    tally: RenderTally
    name: str = "observed"

    def render(self, explanation: Explanation) -> Explanation:
        original = tuple(claim.text for claim in _claims(explanation))
        final = self.verifier.render(explanation)
        surviving = tuple(claim.text for claim in _claims(final))
        proposals = self.recorder.last

        self.tally.explanations += 1
        self.tally.claims += len(original)

        for index, was in enumerate(original):
            proposed = proposals[index] if index < len(proposals) else None
            if proposed is None or proposed == was:
                continue
            self.tally.proposed += 1
            now_is = surviving[index] if index < len(surviving) else was
            if now_is == proposed:
                self.tally.kept += 1
            else:
                self.tally.reverted += 1

        return final


def observed_renderer(inner: Renderer) -> tuple[Any, RenderTally]:
    """Wrap a renderer so its revert rate is countable. Returns `(renderer, tally)`.

    The composition is `observe(verify(record(inner)))`. Output is
    identical to `VerifiedRenderer(inner)` — a test asserts that — so
    turning observation on cannot change what a reader sees, which is the
    property that makes it safe to leave on.
    """
    tally = RenderTally()
    recorder = ProposalRecorder(inner=inner, tally=tally)
    verifier = VerifiedRenderer(inner=recorder)
    return _ObservedVerifier(verifier=verifier, recorder=recorder, tally=tally), tally


def _claims(explanation: Explanation) -> tuple[Any, ...]:
    """Positional claims, in the same order Module 16 matches them."""
    return explanation.claims()
