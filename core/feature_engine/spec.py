"""The versioned feature specification.

## Lookback windows are not duration thresholds

ARGUS's hardest rule at this layer: **no fixed duration thresholds
anywhere**. A consolidation lasting three weeks and one lasting three
years must be measurable by the same features. `if days_in_range > 30`
is the rigid rule the whole architecture was built to avoid.

Every window in this file is a *measurement scale*, not a gate. The
distinction is precise and worth stating because it is easy to blur:

- A **gate** branches on duration to decide what something *is*:
  `if consolidation_days > 30: is_consolidating = True`. Forbidden. No
  feature in this module makes such a branch — nothing here returns a
  classification, and nothing changes behaviour based on how long a
  pattern has lasted.
- A **scale** is the ruler a measurement is taken with: "realized
  volatility over 20 bars". Changing it changes the number's resolution,
  not whether a pattern qualifies. A 3-year consolidation and a 3-week
  one both produce a valid, comparable `volatility_compression` reading
  through the same 20-bar ruler.

Duration itself is a *feature* (`decline_duration_bars`), stored as a
number for downstream modules to weigh — never consumed here as a
condition.

## Why the windows live on the spec

They are part of the versioned schema, hashed into `content_checksum`.
Changing a window changes the checksum, which means a **new
`feature_schema_version`** — never a silent recalculation of an existing
one. That is what keeps a historical signal reproducible: re-running it
with its recorded schema version ID reproduces the identical number.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from infra.db.schema.versioning import feature_schema_version


@dataclass(frozen=True, slots=True)
class FeatureWindows:
    """Measurement scales, in bars of whatever timeframe is being computed.

    Expressed in bars rather than calendar days so the same spec applies
    unchanged to daily, weekly and monthly panels — 20 bars is 20 sessions
    on a daily panel and 20 weeks on a weekly one, which is the correct
    scaling for a multi-timeframe feature set.
    """

    #: Short/medium/long measurement scales, used pervasively.
    short: int = 10
    medium: int = 20
    long: int = 60
    #: The structural lookback for decline/drawdown measurement.
    structural: int = 252
    #: Scale for "is this bar a new local extreme" pivot detection.
    pivot: int = 5
    #: Trailing sample used for percentile-rank features (ATR percentile).
    percentile: int = 252

    @property
    def max_lookback(self) -> int:
        """Longest scale in the spec — how much history a computation needs."""
        return max(
            self.short,
            self.medium,
            self.long,
            self.structural,
            self.pivot,
            self.percentile,
        )


@dataclass(frozen=True, slots=True)
class FeatureTolerances:
    """Proportional tolerances. Also scales, not duration gates.

    These are fractions of price, not counts of days — a level "tested"
    within 1.5% is the same judgement whether the base lasted a month or
    a decade.
    """

    #: How close to a level counts as testing it.
    level_test: float = 0.015
    #: Fraction of a range's height counted as its "upper" portion.
    upper_range: float = 0.25


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """A complete, hashable feature specification.

    `name` plus the hash of every window and tolerance is what a
    `feature_schema_version` row records, so two runs that agree on the
    checksum are guaranteed to have computed features the same way.
    """

    name: str = "argus-features"
    windows: FeatureWindows = field(default_factory=FeatureWindows)
    tolerances: FeatureTolerances = field(default_factory=FeatureTolerances)
    #: Annualisation factor for realized volatility on a daily panel.
    #: Rescaled per timeframe by the engine.
    trading_periods_per_year: int = 252

    def definition(self) -> dict[str, object]:
        """The serializable definition stored on the schema version row."""
        return {
            "name": self.name,
            "windows": asdict(self.windows),
            "tolerances": asdict(self.tolerances),
            "trading_periods_per_year": self.trading_periods_per_year,
            "feature_names": sorted(FEATURE_NAMES),
        }

    def content_checksum(self) -> str:
        """Stable hash over everything that affects a computed value.

        Includes the feature *name list* as well as the parameters: adding
        a feature changes the vector's meaning even if no window moved,
        and must therefore also force a new version.
        """
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"


# --------------------------------------------------------------------------
# The authoritative feature list, by group
# --------------------------------------------------------------------------

#: Group A — Prior Decline & Stabilization.
GROUP_A_FEATURES: tuple[str, ...] = (
    "peak_to_trough_decline",
    "decline_duration_bars",
    "decline_speed",
    "drawdown_pct",
    "momentum_deterioration",
    "rs_deterioration_vs_market",
    "rs_deterioration_vs_sector",
    "distance_from_historical_high",
    "lower_low_frequency",
    "downside_momentum_reduction",
    "volatility_contraction_onset",
)

#: Group B — Consolidation.
GROUP_B_FEATURES: tuple[str, ...] = (
    "normalized_range_width",
    "atr",
    "atr_percentile",
    "realized_volatility",
    "volatility_compression",
    "volume_contraction",
    "rvol",
    "support_test_count",
    "resistance_test_count",
    "failed_breakdown_count",
    "failed_breakout_count",
    "higher_low_development",
    "structure_transition",
)

#: Group C — Awakening.
GROUP_C_FEATURES: tuple[str, ...] = (
    "volatility_reexpansion",
    "volume_expansion",
    "rvol_increase",
    "range_expansion",
    "resistance_pressure",
    "higher_high_frequency",
    "higher_low_frequency",
    "momentum_improvement",
    "rs_improvement_vs_market",
    "rs_improvement_vs_sector",
    "time_in_upper_range",
)

#: Group D — Confirmation.
GROUP_D_FEATURES: tuple[str, ...] = (
    "resistance_breakout_pct",
    "breakout_volume_ratio",
    "acceptance_followthrough",
    "retest_and_hold",
    "higher_high_magnitude",
    "rs_alignment_market",
    "rs_alignment_sector",
)

#: Group E — Context. Computed for every security on every date.
GROUP_E_FEATURES: tuple[str, ...] = (
    "rs_vs_market",
    "rs_vs_sector",
    "rs_vs_industry",
    "market_regime_trend",
    "market_regime_volatility",
    "market_regime_drawdown",
    "avg_dollar_volume",
    "spread_proxy",
)

FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "A_decline": GROUP_A_FEATURES,
    "B_consolidation": GROUP_B_FEATURES,
    "C_awakening": GROUP_C_FEATURES,
    "D_confirmation": GROUP_D_FEATURES,
    "E_context": GROUP_E_FEATURES,
}

FEATURE_NAMES: tuple[str, ...] = tuple(name for group in FEATURE_GROUPS.values() for name in group)


def publish_feature_schema_version(
    connection: Connection,
    spec: FeatureSpec,
    *,
    description: str | None = None,
) -> UUID:
    """Get or create the `feature_schema_version` row for this spec.

    Idempotent by checksum: publishing the same spec twice returns the
    same ID rather than creating a second version. `feature_schema_version`
    is append-only (Module 03), so a genuinely different spec becomes a
    new row and never edits an existing one — which is exactly what makes
    a signal that recorded a schema version ID reproducible.
    """
    checksum = spec.content_checksum()
    existing = connection.execute(
        select(feature_schema_version.c.id)
        .where(feature_schema_version.c.content_checksum == checksum)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    return connection.execute(
        feature_schema_version.insert()
        .values(
            version_label=spec.version_label(),
            definition=spec.definition(),
            content_checksum=checksum,
            description=description,
            published_at=datetime.now(UTC),
        )
        .returning(feature_schema_version.c.id)
    ).scalar_one()
