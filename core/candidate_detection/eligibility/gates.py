"""What a gate returns, and what a full eligibility evaluation returns.

The distinction this whole package exists to preserve, stated once:

- A **low score** says "we evaluated this and it is weak."
- **`INSUFFICIENT_EVIDENCE`** says "we did not evaluate this at all, and
  here is precisely why."

Collapsing the two is the failure mode. A gate that returned a number
instead of a verdict would let a downstream ranking quietly sort an
unevaluatable security among evaluated ones, and false-positive analysis
would no longer be able to tell a model that misreads patterns from a
model that does not know when it is uninformed.

So every gate returns a boolean plus a `detail` payload carrying the
measured value and the threshold it was compared against — never a bare
`False`. `eligibility_check_results.detail` is `NOT NULL` in Module 03's
schema for exactly this reason: a rejection ARGUS cannot explain is a
rejection ARGUS should not make.

Each gate is evaluated **independently and recorded independently**, even
when an earlier one already failed. Short-circuiting would be cheaper and
would destroy the diagnosis: "this security failed liquidity" and "this
security failed liquidity, bankruptcy risk, and data history" are
different facts about the world, and only the second one tells you the
name is hopeless rather than merely untradeable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from infra.db.enums import EligibilityGate, EvidenceStatus


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict on one security, with its reasoning."""

    gate: EligibilityGate
    passed: bool
    #: Measured value, threshold, and anything else needed to re-derive
    #: the verdict. Stored verbatim as JSONB.
    detail: dict[str, Any]

    def __bool__(self) -> bool:
        return self.passed


@dataclass(frozen=True, slots=True)
class EligibilityOutcome:
    """Every gate's verdict for one security."""

    security_id: UUID
    as_of: datetime
    results: dict[EligibilityGate, GateResult]

    @property
    def failed_gates(self) -> tuple[EligibilityGate, ...]:
        """Every gate that failed — not just the first.

        Ordered by the enum's declaration order so a report reads the
        same way every run.
        """
        return tuple(gate for gate in EligibilityGate if not self.results[gate].passed)

    @property
    def eligible(self) -> bool:
        return not self.failed_gates

    @property
    def status(self) -> EvidenceStatus:
        """`SCORED` means *may be scored*, never *has been scored*.

        Module 13 assigns the numbers. This module only decides whether it
        is legitimate to try.
        """
        return EvidenceStatus.SCORED if self.eligible else EvidenceStatus.INSUFFICIENT_EVIDENCE

    def reason_summary(self) -> dict[str, Any]:
        """Why this security is ineligible, in a form a human can read."""
        return {gate.value: self.results[gate].detail for gate in self.failed_gates}


@dataclass(frozen=True, slots=True)
class EligibilityReport:
    """The outcome of evaluating a whole candidate pool."""

    as_of: datetime
    run_id: UUID
    detection_configuration_id: UUID | None
    outcomes: dict[UUID, EligibilityOutcome] = field(default_factory=dict)

    def eligible(self) -> dict[UUID, EligibilityOutcome]:
        return {
            security_id: outcome
            for security_id, outcome in self.outcomes.items()
            if outcome.eligible
        }

    def ineligible(self) -> dict[UUID, EligibilityOutcome]:
        return {
            security_id: outcome
            for security_id, outcome in self.outcomes.items()
            if not outcome.eligible
        }

    def failures_by_gate(self) -> dict[EligibilityGate, int]:
        """How many candidates each gate rejected.

        A gate rejecting nothing over a full universe is either
        misconfigured or redundant; a gate rejecting everything has been
        set too tight. Both are visible here and nowhere else.
        """
        counts = dict.fromkeys(EligibilityGate, 0)
        for outcome in self.outcomes.values():
            for gate in outcome.failed_gates:
                counts[gate] += 1
        return counts

    def __len__(self) -> int:
        return len(self.outcomes)
