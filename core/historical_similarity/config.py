"""Every threshold this module uses, isolated the way Module 10 established.

**None of these values has been statistically validated.** Same status as
Module 10's, and for a sharper reason: the historical CASE dataset is
nearly empty until Module 17's full scan runs, so there is not yet data
against which a similarity radius *could* be calibrated. Saying "0.75 is
the right distance cutoff" today would be a guess dressed as a
measurement.

Every stored result therefore carries
`calibration_status: "UNVALIDATED_PLACEHOLDERS"`, exactly as Module 10
writes into its evidence.

## Kind tagging, carried over from Module 10

- **`structural`** — follows from the metric's construction rather than
  from a judgement about markets. Changing it would change what the
  number *means*, not merely how strict it is.
- **`calibratable`** — an invented magnitude. This is where recalibration
  starts, and where nothing should be trusted.

## How recalibration would work, concretely

1. `SimilarityConfig.from_definition()` rebuilds the exact values a
   stored result was produced under.
2. Change the numbers. Nothing else.
3. `publish_similarity_configuration()` writes a new immutable
   `data_snapshot` row; a different checksum means results computed under
   the old values stay attributable to them.
4. Re-run. `as_of` is a plain argument, so the call that searches today
   searches 2015.

No classification or distance code is touched by any of that.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.historical_similarity.features import exclusion_report, metric_features
from infra.db.schema.versioning import data_snapshot

#: Follows from how the metric is built; changing it changes meaning.
STRUCTURAL = "structural"
#: An invented magnitude. Recalibration starts here.
CALIBRATABLE = "calibratable"


@dataclass(frozen=True, slots=True)
class SimilarityThreshold:
    """One named threshold, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _t(value: float, kind: str, rationale: str) -> SimilarityThreshold:
    return SimilarityThreshold(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class SimilarityThresholds:
    """The complete threshold set. Enumerable as a whole."""

    max_distance: SimilarityThreshold = field(
        default_factory=lambda: _t(
            1.25,
            CALIBRATABLE,
            "Maximum scaled distance for a historical case to count as an "
            "analogue. In robust-scaled units, 1.0 means 'differs by about "
            "one interquartile range on a typical feature'. Entirely "
            "invented — there is no dataset yet against which a radius "
            "could be validated.",
        )
    )
    min_feature_overlap: SimilarityThreshold = field(
        default_factory=lambda: _t(
            0.60,
            CALIBRATABLE,
            "Fraction of metric features that must be present in BOTH "
            "vectors for a distance to be computed at all. Below this the "
            "pair is reported as incomparable rather than assigned a "
            "distance over whatever few features happened to overlap.",
        )
    )
    min_samples_for_statistics: SimilarityThreshold = field(
        default_factory=lambda: _t(
            5.0,
            CALIBRATABLE,
            "Below this many analogues, no outcome statistic is reported at "
            "all — the result is INSUFFICIENT. A median of two numbers is "
            "arithmetic, not evidence, and reporting it would imply "
            "confidence the sample cannot support.",
        )
    )
    preferred_samples: SimilarityThreshold = field(
        default_factory=lambda: _t(
            30.0,
            CALIBRATABLE,
            "Above this, statistics are reported without a sparse-sample "
            "caveat. Between the two floors they are reported AND flagged "
            "SPARSE. 30 is the conventional rule-of-thumb boundary and is "
            "cited here as convention, not as a finding about markets.",
        )
    )
    scale_floor: SimilarityThreshold = field(
        default_factory=lambda: _t(
            1e-9,
            STRUCTURAL,
            "Lower bound on a feature's robust scale before dividing. "
            "Guards a constant feature, whose IQR is zero — without it a "
            "single degenerate column would make every distance infinite.",
        )
    )

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name).value) for f in fields(self)}

    def describe(self) -> dict[str, dict[str, Any]]:
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    def calibratable(self) -> dict[str, float]:
        """Only the invented magnitudes — where recalibration starts."""
        return {
            f.name: float(getattr(self, f.name).value)
            for f in fields(self)
            if getattr(self, f.name).kind == CALIBRATABLE
        }

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> SimilarityThresholds:
        stored = definition.get("thresholds", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = SimilarityThreshold(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)


@dataclass(frozen=True, slots=True)
class SimilarityConfig:
    """The module's complete, versioned configuration."""

    name: str = "argus-similarity"
    thresholds: SimilarityThresholds = field(default_factory=SimilarityThresholds)

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "metric": "robust_scaled_euclidean",
            "thresholds": self.thresholds.as_dict(),
            "metric_features": list(metric_features()),
            "excluded_features": exclusion_report(),
            "calibration_status": "UNVALIDATED_PLACEHOLDERS",
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"


def publish_similarity_configuration(
    connection: Connection,
    config: SimilarityConfig | None = None,
    *,
    as_of: datetime,
    description: str | None = None,
) -> UUID:
    """Get or create the `data_snapshot` row for this configuration and `as_of`.

    `historical_similarity_results.data_snapshot_id` is `NOT NULL`, and
    Module 03's comment on `data_snapshot` says why: re-running a
    computation against the snapshot must see exactly the rows whose
    `availability_time <= as_of`. So the snapshot pins both the PIT cutoff
    and the configuration that produced the result — the pair is what
    makes a historical similarity result reproducible.

    Idempotent by checksum, matching Modules 08, 09 and 10.
    """
    config = config or SimilarityConfig()
    # The cutoff is part of the identity: the same configuration at a
    # different `as_of` searches a different dataset and is a different
    # snapshot.
    checksum = hashlib.sha256(
        f"{config.content_checksum()}|{as_of.isoformat()}".encode()
    ).hexdigest()

    existing = connection.execute(
        select(data_snapshot.c.id).where(data_snapshot.c.content_checksum == checksum).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    return connection.execute(
        data_snapshot.insert()
        .values(
            version_label=f"{config.version_label()}@{as_of.date().isoformat()}",
            as_of_time=as_of,
            definition=config.definition(),
            content_checksum=checksum,
            description=description or "Unvalidated placeholder thresholds (Module 11).",
            published_at=datetime.now(UTC),
        )
        .returning(data_snapshot.c.id)
    ).scalar_one()
