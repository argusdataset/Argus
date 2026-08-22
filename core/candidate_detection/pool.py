"""What candidate detection returns.

A `CandidatePool` is the answer to one question — *"could this contain a
setup of interest?"* — and deliberately nothing more. It carries no
score, no classification, and no ranking that downstream modules are
meant to trust as a quality judgement.

`composite_rank` exists because the pre-filter has to cut somewhere, and
cutting requires an order. It is a within-batch percentile, not a
measure of how good a setup is, and it is named to make borrowing it as
one awkward. Module 13 scores; this module selects who gets looked at.

Securities that did not make the pool are kept in `excluded`, with a
reason. Detection is a filter, and a filter that discards the record of
what it filtered cannot be audited — which is the same reason Module 08
reports `missing_securities` instead of quietly returning fewer vectors
than it was asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ExclusionReason(StrEnum):
    """Why a security did not enter the candidate pool.

    Detection-stage reasons only. Eligibility failures are recorded
    separately, per gate, in `eligibility_check_results` — a security can
    perfectly well be a candidate and still be ineligible, and collapsing
    the two would destroy exactly the distinction Module 09 exists to
    maintain.
    """

    #: No feature vector at all — Module 08 reported it missing.
    NO_FEATURES = "no_features"
    #: Too few ranking components computed to form a composite.
    INSUFFICIENT_COMPONENTS = "insufficient_components"
    #: Has not declined from its structural peak, so the setup's first
    #: phase is absent by definition.
    NOT_OFF_ITS_PEAK = "not_off_its_peak"
    #: Ranked below the selection cut.
    BELOW_SELECTION_CUT = "below_selection_cut"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One security that passed the coarse pre-filter."""

    security_id: UUID
    #: Within-batch percentile of the composite, 0..1, higher = more
    #: setup-like on the coarse measures. An ordering device. Not a score.
    composite_rank: float
    #: The per-component percentile ranks that produced it, so a
    #: candidate's presence in the pool is explainable rather than a bare
    #: boolean — every ARGUS output must be traceable to named parts.
    components: dict[str, float]
    #: How many components were actually available.
    components_available: int


@dataclass(frozen=True, slots=True)
class CandidatePool:
    """The output of one detection pass over one `as_of`."""

    as_of: datetime
    run_id: UUID
    detection_configuration_id: UUID | None
    candidates: dict[UUID, Candidate]
    #: Every security considered and not selected, with why.
    excluded: dict[UUID, ExclusionReason] = field(default_factory=dict)

    @property
    def considered(self) -> int:
        """Securities the pre-filter actually looked at."""
        return len(self.candidates) + len(self.excluded)

    @property
    def reduction_ratio(self) -> float:
        """Pool size as a fraction of everything considered.

        The number Module 09 is judged on: a coarse pre-filter that does
        not meaningfully reduce the universe has not earned its place in
        the pipeline.
        """
        if self.considered == 0:
            return 0.0
        return len(self.candidates) / self.considered

    def ranked(self) -> list[Candidate]:
        """Candidates ordered most setup-like first."""
        return sorted(self.candidates.values(), key=lambda c: c.composite_rank, reverse=True)

    def __len__(self) -> int:
        return len(self.candidates)

    def __contains__(self, security_id: object) -> bool:
        return security_id in self.candidates
