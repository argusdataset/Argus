"""When a setup's thesis has stopped holding — surfaced, not re-derived.

Two invalidation signals, and neither is computed here:

* **A backward state transition.** Module 10 records it, flags its
  direction at write time, and counts it. This module reads
  `backward_transition_count()` and `history()` and does no
  reinterpretation — deliberately, because Module 10's report was
  explicit that direction is stored rather than recomputed so a future
  reordering of `CYCLE_ORDER` cannot retroactively change what a
  historical row meant. Re-deriving direction here would reintroduce
  exactly that.
* **Lost eligibility.** Module 09 writes one row per (run, security,
  gate) into `eligibility_check_results`, passes included — its
  persistence docstring says storing only failures would make "evaluated
  and passed" indistinguishable from "never evaluated". That decision is
  what makes this signal computable at all, and this module is its first
  consumer. Module 09 has no reader; `eligibility_history` below is one,
  and it does not re-run a single gate.

## Why re-evaluation is a different question from evaluation

Module 09 answers "is this security eligible now". The risk question is
"was it eligible, and is it still" — a security that passed six gates in
March and fails the liquidity gate in June has had something happen to
it, and that is actionable in a way a security that never passed is not.
The difference lives entirely in the history, which is why this reads
across runs rather than calling the gate again.

## Confidence is not read

Module 10's `confidence` is available on every transition row and is
deliberately ignored here, per that module's own warning: it is a
pattern-match score from unvalidated weights, and treating it as a risk
signal would be a misuse. State facts and transition counts only.

## No blended invalidation score

The Module 12 brief is explicit, and it matches Module 11's structural
separation: these are two different kinds of evidence about two different
things. `signals_raised()` returns the *names* that fired. Weighing them
against each other is Module 13's job, if it happens at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.market_state.transitions import Transition, backward_transition_count, history
from infra.db.enums import EligibilityGate, MarketState
from infra.db.schema.intelligence import eligibility_check_results

#: Names of the invalidation signals, for the same reason the flag names
#: are constants.
BACKWARD_TRANSITION = "backward_transition"
LOST_ELIGIBILITY = "lost_eligibility"

INVALIDATION_SIGNALS: tuple[str, ...] = (BACKWARD_TRANSITION, LOST_ELIGIBILITY)


class EligibilityTrend(StrEnum):
    """What the eligibility record says across runs, not within one."""

    #: No gate result has ever been recorded for this security.
    NEVER_EVALUATED = "never_evaluated"
    #: The most recent evaluation passed every gate.
    PASSING = "passing"
    #: The most recent evaluation failed, and an earlier one passed. The
    #: actionable case: something changed.
    LOST_ELIGIBILITY = "lost_eligibility"
    #: Failed most recently and never passed. Not an invalidation — it
    #: was never valid.
    NEVER_ELIGIBLE = "never_eligible"


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    """One run's verdict on one security, assembled from its gate rows."""

    run_id: UUID
    evaluated_at: datetime
    failed_gates: tuple[EligibilityGate, ...]
    gates_recorded: int

    @property
    def passed(self) -> bool:
        return not self.failed_gates


@dataclass(frozen=True, slots=True)
class EligibilityChange:
    """How eligibility moved, across the runs knowable at `as_of`."""

    trend: EligibilityTrend
    latest: EligibilityVerdict | None = None
    previous: EligibilityVerdict | None = None
    #: When this security last passed every gate, if it ever did.
    last_passing_at: datetime | None = None
    #: Gates failing now that passed in the immediately preceding run.
    #: The specific thing that broke, rather than everything now failing.
    newly_failed_gates: tuple[EligibilityGate, ...] = ()
    runs_observed: int = 0

    @property
    def lost(self) -> bool:
        return self.trend is EligibilityTrend.LOST_ELIGIBILITY

    def as_dict(self) -> dict[str, Any]:
        return {
            "trend": self.trend.value,
            "runs_observed": self.runs_observed,
            "last_passing_at": (self.last_passing_at.isoformat() if self.last_passing_at else None),
            "failed_gates": [gate.value for gate in self.latest.failed_gates]
            if self.latest
            else [],
            "newly_failed_gates": [gate.value for gate in self.newly_failed_gates],
            "latest_evaluated_at": (self.latest.evaluated_at.isoformat() if self.latest else None),
        }


@dataclass(frozen=True, slots=True)
class InvalidationSignals:
    """Both invalidation inputs for one security, side by side and unmixed."""

    security_id: UUID
    as_of: datetime
    #: The state as of this instant, from the transition log rather than
    #: the `market_state` projection — the projection holds *now*, which
    #: is the wrong answer during a replay.
    current_state: MarketState | None
    #: Module 10's count, read from the flag it stored at write time.
    backward_transitions: int
    #: When the most recent retreat happened. An old scar and an active
    #: invalidation are different, and only the timestamp separates them.
    last_backward_at: datetime | None
    last_transition: Transition | None
    transitions_observed: int
    eligibility: EligibilityChange

    def signals_raised(self) -> tuple[str, ...]:
        """Which named signals fired. A list, never a total."""
        raised = []
        if self.backward_transitions:
            raised.append(BACKWARD_TRANSITION)
        if self.eligibility.lost:
            raised.append(LOST_ELIGIBILITY)
        return tuple(raised)

    def as_dict(self) -> dict[str, Any]:
        return {
            "current_state": self.current_state.value if self.current_state else None,
            "backward_transitions": self.backward_transitions,
            "last_backward_at": (
                self.last_backward_at.isoformat() if self.last_backward_at else None
            ),
            "transitions_observed": self.transitions_observed,
            "eligibility": self.eligibility.as_dict(),
            "signals_raised": list(self.signals_raised()),
            "note": (
                "Module 10 confidence is deliberately not read — it is an "
                "unvalidated pattern-match score, not a risk signal."
            ),
        }


def assess_invalidation(
    connection: Connection,
    security_id: UUID,
    *,
    as_of: datetime,
) -> InvalidationSignals:
    """Both invalidation signals for one security, bounded by `as_of`."""
    transitions = history(connection, security_id, as_of=as_of)
    backward = [t for t in transitions if t.is_backward]

    return InvalidationSignals(
        security_id=security_id,
        as_of=as_of,
        current_state=transitions[-1].to_state if transitions else None,
        # The count comes from Module 10's stored flag and is
        # authoritative. `is_backward` above only locates *which* rows to
        # timestamp; if the two ever disagreed, the count is the one to
        # trust, because it reflects what was recorded at the time.
        backward_transitions=backward_transition_count(connection, security_id, as_of=as_of),
        last_backward_at=backward[-1].transition_time if backward else None,
        last_transition=transitions[-1] if transitions else None,
        transitions_observed=len(transitions),
        eligibility=assess_eligibility_change(connection, security_id, as_of=as_of),
    )


# --------------------------------------------------------------------------
# Reading Module 09's record
# --------------------------------------------------------------------------


def eligibility_history(
    connection: Connection,
    security_id: UUID,
    *,
    as_of: datetime | None = None,
) -> list[EligibilityVerdict]:
    """Every recorded eligibility verdict for one security, oldest first.

    Bounded by `evaluated_at`, which Module 09's persistence sets to the
    report's `as_of` rather than to wall-clock insert time — so the bound
    is on when the evaluation *was about*, which is the coordinate a
    replay needs.

    Rows are per gate; a verdict is the whole run's set of them. A run
    that recorded fewer gates than the enum defines is reported with its
    `gates_recorded` count rather than being silently treated as a pass,
    because a partially-recorded run is not evidence of eligibility.
    """
    query = select(
        eligibility_check_results.c.run_id,
        eligibility_check_results.c.gate,
        eligibility_check_results.c.passed,
        eligibility_check_results.c.evaluated_at,
    ).where(eligibility_check_results.c.security_id == security_id)
    if as_of is not None:
        query = query.where(eligibility_check_results.c.evaluated_at <= as_of)

    runs: dict[UUID, dict[str, Any]] = {}
    for row in connection.execute(query.order_by(eligibility_check_results.c.evaluated_at)):
        entry = runs.setdefault(
            row.run_id, {"evaluated_at": row.evaluated_at, "failed": [], "count": 0}
        )
        entry["count"] += 1
        entry["evaluated_at"] = max(entry["evaluated_at"], row.evaluated_at)
        if not row.passed:
            entry["failed"].append(EligibilityGate(row.gate))

    verdicts = [
        EligibilityVerdict(
            run_id=run_id,
            evaluated_at=entry["evaluated_at"],
            failed_gates=tuple(gate for gate in EligibilityGate if gate in set(entry["failed"])),
            gates_recorded=entry["count"],
        )
        for run_id, entry in runs.items()
    ]
    verdicts.sort(key=lambda verdict: verdict.evaluated_at)
    return verdicts


def assess_eligibility_change(
    connection: Connection,
    security_id: UUID,
    *,
    as_of: datetime | None = None,
) -> EligibilityChange:
    """Whether this security has lost eligibility it previously held."""
    verdicts = eligibility_history(connection, security_id, as_of=as_of)
    if not verdicts:
        return EligibilityChange(trend=EligibilityTrend.NEVER_EVALUATED)

    latest = verdicts[-1]
    previous = verdicts[-2] if len(verdicts) > 1 else None
    passing = [verdict for verdict in verdicts if verdict.passed]
    last_passing_at = passing[-1].evaluated_at if passing else None

    if latest.passed:
        trend = EligibilityTrend.PASSING
    elif any(verdict.passed for verdict in verdicts[:-1]):
        trend = EligibilityTrend.LOST_ELIGIBILITY
    else:
        trend = EligibilityTrend.NEVER_ELIGIBLE

    previously_failed = set(previous.failed_gates) if previous else set()
    newly_failed = tuple(
        gate
        for gate in latest.failed_gates
        if previous is not None and gate not in previously_failed
    )

    return EligibilityChange(
        trend=trend,
        latest=latest,
        previous=previous,
        last_passing_at=last_passing_at,
        newly_failed_gates=newly_failed,
        runs_observed=len(verdicts),
    )
