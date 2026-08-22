"""A synthetic universe of feature vectors, with known populations planted in it.

Module 09's tests need a universe, not a security. The claims under test
are population-level — "the pool is an order of magnitude smaller than the
universe", "the liquidity floor does not delete thinly-traded small-caps"
— and neither can be checked against a single fixture.

So this builds a few hundred feature vectors with deliberately mixed
profiles, and plants named individuals inside them whose correct treatment
is known in advance:

- `BASING_ID` — a textbook base: deep drawdown, compressed volatility,
  tight range, thin-but-tradeable volume. Must reach the pool.
- `AT_HIGHS_ID` — making new highs. Must be excluded before ranking.
- `THIN_SMALLCAP_ID` — the MLSS/SLS/QBTS profile: a genuine base on very
  low dollar volume. Must reach the pool *and* clear the liquidity gate.
- `UNTRADEABLE_ID` — dollar volume so low a position would be the tape.
  Must fail liquidity.
- `SPARSE_ID` — a recently-listed security: short history, most long-window
  features absent. Must fail data history and data quality.

Vectors are constructed directly rather than computed from prices by
Module 08. That is deliberate: these tests are about Module 09's
decisions, and routing through a price panel would make a failure
ambiguous between "the gate is wrong" and "the synthetic prices did not
produce the feature values I assumed". The integration tests do use real
Module 08 output, against a real database, where that end-to-end coupling
is the thing being checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

import numpy as np

from core.data_validation.result import MissReason
from core.feature_engine.spec import FEATURE_NAMES, FeatureSpec
from core.feature_engine.vector import (
    BatchFeatureResult,
    FeatureEvidence,
    FeatureVector,
)
from data.canonical_model.records import CanonicalTimeframe

_NAMESPACE = UUID("c9d2b8f3-0000-4000-8000-000000000000")

BASING_ID = uuid5(_NAMESPACE, "basing")
AT_HIGHS_ID = uuid5(_NAMESPACE, "at-highs")
THIN_SMALLCAP_ID = uuid5(_NAMESPACE, "thin-smallcap")
UNTRADEABLE_ID = uuid5(_NAMESPACE, "untradeable")
SPARSE_ID = uuid5(_NAMESPACE, "sparse")

PLANTED = (BASING_ID, AT_HIGHS_ID, THIN_SMALLCAP_ID, UNTRADEABLE_ID, SPARSE_ID)

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)
SEED = 20260822

#: Big enough that a 10% selection fraction is a meaningful cut and the
#: percentile ranks are not dominated by a handful of securities.
UNIVERSE_SIZE = 400

FULL_HISTORY = FeatureSpec().windows.max_lookback


@dataclass(frozen=True, slots=True)
class Profile:
    """The handful of features Module 09 actually reads."""

    drawdown_pct: float
    volatility_compression: float
    normalized_range_width: float
    volume_contraction: float
    atr_percentile: float
    avg_dollar_volume: float


#: The planted individuals. Values chosen so each one's correct treatment
#: follows from the gate definitions rather than from tuning.
PLANTED_PROFILES: dict[UUID, Profile] = {
    # Textbook base: deep decline, quiet, tight, thin volume, liquid enough.
    BASING_ID: Profile(-0.62, 0.35, 0.06, 0.45, 0.04, 3_000_000.0),
    # Making new highs — no prior decline, so not this setup by definition.
    AT_HIGHS_ID: Profile(0.0, 1.4, 0.30, 1.6, 0.92, 12_000_000.0),
    # The names ARGUS exists to find: a real base on a microcap tape.
    THIN_SMALLCAP_ID: Profile(-0.71, 0.31, 0.05, 0.40, 0.03, 90_000.0),
    # Below the execution-feasibility floor.
    UNTRADEABLE_ID: Profile(-0.68, 0.33, 0.055, 0.42, 0.035, 8_000.0),
    # Recently listed, and base-like on the features it DOES have — a
    # recent IPO that fell and is now consolidating. Deliberately
    # constructed to rank into the pool, because a sparse security that
    # detection quietly cut would never exercise the gates that exist to
    # catch it. Eligibility, not detection, is what must reject this.
    SPARSE_ID: Profile(float("nan"), 0.30, 0.05, 0.38, float("nan"), 400_000.0),
}


def build_universe(
    *,
    size: int = UNIVERSE_SIZE,
    seed: int = SEED,
    as_of: datetime = AS_OF,
) -> BatchFeatureResult:
    """A `BatchFeatureResult` of `size` securities with the planted names inside.

    The background population is drawn from distributions wide enough that
    the planted basing securities genuinely have to compete — a background
    of uniformly dull securities would make "the base ranked highly" true
    by construction rather than by the ranking working.
    """
    rng = np.random.default_rng(seed)
    vectors: dict[UUID, FeatureVector] = {}

    for security_id, profile in PLANTED_PROFILES.items():
        sparse = security_id is SPARSE_ID
        vectors[security_id] = _vector(
            security_id,
            profile,
            as_of=as_of,
            bars_available=30 if sparse else FULL_HISTORY + 500,
            drop_long_window_features=sparse,
        )

    for index in range(size - len(PLANTED_PROFILES)):
        security_id = uuid5(_NAMESPACE, f"background-{index}")
        vectors[security_id] = _vector(
            security_id,
            _background_profile(rng),
            as_of=as_of,
            bars_available=FULL_HISTORY + 500,
            drop_long_window_features=False,
        )

    return BatchFeatureResult(
        as_of=as_of,
        feature_schema_version_id=None,
        timeframe=CanonicalTimeframe.DAILY,
        vectors=vectors,
        missing_securities={},
    )


def _background_profile(rng: np.random.Generator) -> Profile:
    """A broad spread — some declining and quiet, most not.

    Roughly a fifth of the background is drawn from a basing-like
    distribution, so the pool is competitive and the selection cut is
    doing real work rather than skimming the only five interesting names.
    """
    basing_like = rng.random() < 0.20
    if basing_like:
        return Profile(
            drawdown_pct=float(rng.uniform(-0.75, -0.20)),
            volatility_compression=float(rng.uniform(0.3, 0.9)),
            normalized_range_width=float(rng.uniform(0.05, 0.20)),
            volume_contraction=float(rng.uniform(0.4, 1.0)),
            atr_percentile=float(rng.uniform(0.02, 0.40)),
            avg_dollar_volume=float(10 ** rng.uniform(5.0, 8.5)),
        )
    return Profile(
        drawdown_pct=float(rng.uniform(-0.40, 0.0)),
        volatility_compression=float(rng.uniform(0.8, 2.2)),
        normalized_range_width=float(rng.uniform(0.12, 0.60)),
        volume_contraction=float(rng.uniform(0.8, 2.0)),
        atr_percentile=float(rng.uniform(0.25, 1.0)),
        avg_dollar_volume=float(10 ** rng.uniform(5.5, 9.0)),
    )


def _vector(
    security_id: UUID,
    profile: Profile,
    *,
    as_of: datetime,
    bars_available: int,
    drop_long_window_features: bool,
) -> FeatureVector:
    """One feature vector with every declared feature keyed.

    Features Module 09 does not read are filled with a plausible constant
    rather than left absent, so `completeness` reflects the security's
    actual data situation rather than the fixture's laziness. The sector
    features are the exception: they are `None` for everything, exactly as
    Module 08 reports them, since no sector data exists in the schema.
    """
    features: dict[str, float | None] = {}
    unavailable: list[str] = []

    profile_values = {
        "drawdown_pct": profile.drawdown_pct,
        "volatility_compression": profile.volatility_compression,
        "normalized_range_width": profile.normalized_range_width,
        "volume_contraction": profile.volume_contraction,
        "atr_percentile": profile.atr_percentile,
        "avg_dollar_volume": profile.avg_dollar_volume,
    }

    #: Features whose windows a 30-bar security cannot fill.
    long_window = {
        "peak_to_trough_decline",
        "decline_duration_bars",
        "decline_speed",
        "drawdown_pct",
        "distance_from_historical_high",
        "atr_percentile",
        "rs_deterioration_vs_market",
        "market_regime_drawdown",
    }

    for name in FEATURE_NAMES:
        if "sector" in name or "industry" in name:
            features[name] = None
            unavailable.append(name)
            continue
        if drop_long_window_features and name in long_window:
            features[name] = None
            unavailable.append(name)
            continue

        value = profile_values.get(name)
        if value is None:
            features[name] = 0.5  # a feature this module does not read
            continue
        if np.isnan(value):
            features[name] = None
            unavailable.append(name)
            continue
        features[name] = float(value)

    missing_inputs = {
        "sector_benchmark": MissReason.NEVER_INGESTED,
        "industry_benchmark": MissReason.NEVER_INGESTED,
    }

    return FeatureVector(
        security_id=security_id,
        as_of=as_of,
        feature_schema_version_id=None,
        timeframe=CanonicalTimeframe.DAILY,
        event_time=as_of,
        availability_time=as_of,
        features=features,
        evidence=FeatureEvidence(
            bars_available=bars_available,
            bars_required=FULL_HISTORY,
            missing_inputs=missing_inputs,
            unavailable_features=tuple(unavailable),
        ),
    )
