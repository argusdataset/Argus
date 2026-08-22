"""The feature vector, and the evidence record that travels with it.

Module 07 established that a missing input is an explicit, checkable
state — `AsOfResult.found is False` with a `MissReason` — never a bare
`None` and never a silent fallback. This module has to keep that promise
across a transformation: many inputs go in, one vector comes out, and
"some of the inputs weren't there" must survive rather than dissolving
into a plausible-looking number.

That matters concretely for Module 09. Its `INSUFFICIENT_EVIDENCE` gate
exists so a candidate is never assigned a score built on absent data —
and it can only make that call if this module tells it honestly what was
missing. A feature vector that quietly zero-filled a 60-bar window with
12 bars of history would look exactly like a real one.

So `FeatureVector` carries `evidence`, and an unavailable feature is
`None` — never 0.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from core.data_validation.result import MissReason
from data.canonical_model.records import CanonicalTimeframe


@dataclass(frozen=True, slots=True)
class FeatureEvidence:
    """What was actually available to compute a vector, and what wasn't."""

    #: Bars of history the panel held for this security.
    bars_available: int
    #: Bars the longest window in the spec needs to produce a full reading.
    bars_required: int
    #: Inputs that were absent, and why. Keyed by input name
    #: ("sector_benchmark", "market_benchmark", "price_history", ...).
    missing_inputs: dict[str, MissReason] = field(default_factory=dict)
    #: Feature names that could not be computed and are therefore None.
    unavailable_features: tuple[str, ...] = ()

    @property
    def has_sufficient_history(self) -> bool:
        """Whether the longest window could actually be filled.

        A statement about *coverage*, not a quality judgement and not a
        gate — Module 09 decides what to do about it. This module only
        reports it truthfully.
        """
        return self.bars_available >= self.bars_required

    @property
    def coverage_ratio(self) -> float:
        """Fraction of the required history that was present, capped at 1."""
        if self.bars_required <= 0:
            return 1.0
        return min(1.0, self.bars_available / self.bars_required)

    @property
    def is_complete(self) -> bool:
        """No missing inputs and no uncomputable features."""
        return (
            self.has_sufficient_history
            and not self.missing_inputs
            and not self.unavailable_features
        )


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """One security's features at one point in time.

    `features` maps every name in the spec to a float or to **None** —
    None meaning "could not be computed from what was available", which
    is categorically different from a computed 0.0.
    """

    security_id: UUID
    as_of: datetime
    feature_schema_version_id: UUID | None
    timeframe: CanonicalTimeframe
    #: The bar this vector describes — the last bar at or before `as_of`.
    event_time: datetime | None
    #: Max `availability_time` across the inputs, per Module 03's
    #: definition of `feature_vectors.availability_time`. Carrying it
    #: means a vector's PIT-correctness is checkable without re-deriving
    #: its entire input set.
    availability_time: datetime | None
    features: dict[str, float | None]
    evidence: FeatureEvidence

    def available_features(self) -> dict[str, float]:
        """Only the features that actually computed. Never zero-filled."""
        return {name: value for name, value in self.features.items() if value is not None}

    def __len__(self) -> int:
        return len(self.available_features())


@dataclass(frozen=True, slots=True)
class BatchFeatureResult:
    """Feature vectors for many securities at one `as_of`."""

    as_of: datetime
    feature_schema_version_id: UUID | None
    timeframe: CanonicalTimeframe
    vectors: dict[UUID, FeatureVector]
    #: Securities requested but absent from the panel entirely — no bars
    #: were knowable as of this date. Reported, never silently dropped.
    missing_securities: dict[UUID, MissReason] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.vectors)

    def __iter__(self):
        return iter(self.vectors.values())

    def with_sufficient_history(self) -> dict[UUID, FeatureVector]:
        """The subset whose longest window was actually fillable.

        A convenience for callers, not a filter this module applies on
        its own — every requested security still appears in `vectors` or
        `missing_securities`.
        """
        return {
            security_id: vector
            for security_id, vector in self.vectors.items()
            if vector.evidence.has_sufficient_history
        }
