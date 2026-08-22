"""The versioned detection and eligibility configuration.

Same discipline as Module 08's `FeatureSpec`: every constant that affects
who reaches scoring is hashed into a `content_checksum`, published as an
immutable `detection_configuration` row, and cited by every gate result.
A recorded configuration ID re-runs identically.

## Which constants are allowed to be absolute, and which are not

Module 08's rule — no fixed duration thresholds, every window a
measurement scale — carries into this module, but it needs one honest
refinement, because two genuinely different kinds of constant live here.

**Pattern judgments must be relative.** "Is this security's base tight
enough to be interesting" has no absolute answer: it depends on the
market, the sector, the era. Anything of that shape is expressed as a
*cross-sectional percentile within the batch* (`selection_fraction`) or
as a *sign condition* (`drawdown_pct < 0` — off its peak, which is a
definitional boundary rather than a tunable magnitude). There is no
`if consolidation_days > 30` and no `if drawdown < -0.30` anywhere in
this module.

**Execution feasibility is absolute.** "Can a position actually be
filled" is a question about dollars, and dollars do not rank
cross-sectionally. A percentile-based liquidity floor would exclude the
bottom N% of the universe *by construction*, every single day, however
liquid that bottom slice actually was — which in a universe of 10,000
mostly-tradeable names would silently discard hundreds of perfectly
executable securities. So `min_avg_dollar_volume` is a real dollar
figure, deliberately, and it is the one absolute magnitude in the module.
Its value is argued for in `eligibility/liquidity.py`.

The distinction is worth stating because blurring it in either direction
does damage: relativizing the liquidity floor would defeat ARGUS's
purpose, and absolutizing the pattern thresholds would defeat Module 08's.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from infra.db.schema.versioning import detection_configuration


@dataclass(frozen=True, slots=True)
class DetectionParameters:
    """How coarse the pre-filter is. A compute budget, not a judgement."""

    #: Fraction of the ranked universe kept as candidates. This is the
    #: high-recall knob: it says "the more expensive downstream stages can
    #: afford to look at this many securities", not "this many securities
    #: have good setups". Deliberately generous — Module 09's failure mode
    #: that actually matters is dropping a real candidate, not passing a
    #: dull one through to a stage that will reject it.
    selection_fraction: float = 0.10

    #: How many of the ranking components must be present for a security
    #: to be ranked at all. Below this there is nothing to rank on, and a
    #: composite built from one component is not a composite. Securities
    #: excluded for this reason are *reported*, never silently dropped.
    min_ranking_components: int = 2


@dataclass(frozen=True, slots=True)
class EligibilityParameters:
    """The six gates' parameters. See each gate module for the reasoning."""

    #: Fraction of the longest feature window that must actually be
    #: covered by real bars. Relative to Module 08's spec rather than an
    #: absolute bar count, so changing a feature window cannot silently
    #: change what this gate means.
    min_history_coverage: float = 1.0

    #: Fraction of the feature set that must have computed. A candidate
    #: assembled mostly from absences is not a candidate.
    min_feature_completeness: float = 0.70

    #: Dollars of average daily volume. The one absolute magnitude in the
    #: module — see this file's docstring and `eligibility/liquidity.py`.
    min_avg_dollar_volume: float = 50_000.0

    #: How many independent distress signals must fire before the
    #: bankruptcy gate excludes a security. Two, not one, because
    #: pre-revenue biotechs and early-stage technology companies —
    #: precisely the names ARGUS exists to find — legitimately trip a
    #: single signal as a matter of course.
    min_distress_signals_to_exclude: int = 2

    #: How many analogues the lightweight check must find. Provisional
    #: alongside the check itself — see `eligibility/analogues.py`.
    min_historical_analogues: int = 5


@dataclass(frozen=True, slots=True)
class DetectionConfig:
    """A complete, hashable detection + eligibility configuration."""

    name: str = "argus-detection"
    detection: DetectionParameters = field(default_factory=DetectionParameters)
    eligibility: EligibilityParameters = field(default_factory=EligibilityParameters)

    def definition(self) -> dict[str, object]:
        return {
            "name": self.name,
            "detection": asdict(self.detection),
            "eligibility": asdict(self.eligibility),
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"


def publish_detection_configuration(
    connection: Connection,
    config: DetectionConfig,
    *,
    description: str | None = None,
) -> UUID:
    """Get or create the `detection_configuration` row for this config.

    Idempotent by checksum, exactly like Module 08's
    `publish_feature_schema_version`. `detection_configuration` is
    append-only, so a changed configuration becomes a new row and never
    edits an existing one — which is what lets a stored gate result be
    re-derived years later from the ID it cites.
    """
    checksum = config.content_checksum()
    existing = connection.execute(
        select(detection_configuration.c.id)
        .where(detection_configuration.c.content_checksum == checksum)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    return connection.execute(
        detection_configuration.insert()
        .values(
            version_label=config.version_label(),
            definition=config.definition(),
            content_checksum=checksum,
            description=description,
            published_at=datetime.now(UTC),
        )
        .returning(detection_configuration.c.id)
    ).scalar_one()
