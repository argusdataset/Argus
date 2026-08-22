"""Feature vectors for a security walking the full setup, phase by phase.

Module 08 already has a synthetic lifecycle panel that produces real
features from real prices. This reuses it rather than inventing a second
one: the question here is whether the *classifier* moves sensibly as a
security progresses, and running it against Module 08's actual computed
features is the only way that question is about the classifier rather than
about my ability to guess plausible feature values.

The MLSS / SLS / HIVE / ALX / QBTS shapes the brief asks about are all the
same structure — a long decline into a quiet base, then an awakening —
differing in depth, duration and noise. `phase_vectors()` takes the
lifecycle panel and produces one `BatchFeatureResult` per phase, so the
progression can be classified as a sequence of `as_of` dates.

That is also the honest way to test a dual-mode interface: each phase is
literally the same call with a different `as_of`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import numpy as np
import pandas as pd

from core.data_validation.result import MissReason
from core.feature_engine.groups import awakening, confirmation, consolidation, context, decline
from core.feature_engine.spec import FEATURE_NAMES, FeatureSpec
from core.feature_engine.vector import (
    BatchFeatureResult,
    FeatureEvidence,
    FeatureVector,
)
from data.canonical_model.records import CanonicalTimeframe
from tests.unit.feature_engine.lifecycle import (
    LIFECYCLE_ID,
    MARKET_ID,
    build_lifecycle_panel,
)

#: The phases, in the order the setup unfolds.
PHASE_ORDER: tuple[str, ...] = (
    "prior_advance",
    "decline",
    "stabilization",
    "consolidation",
    "awakening",
    "confirmation",
)

FULL_HISTORY = FeatureSpec().windows.max_lookback


def phase_frames() -> tuple[dict[str, pd.DataFrame], dict[str, int]]:
    """Every Module 08 feature as a wide frame, plus the phase-end rows.

    Computed once over the whole panel; each phase then reads one row.
    That mirrors how the engine actually runs — features for a date, then
    a classification for that date.
    """
    panel, phase_index = build_lifecycle_panel()
    spec = FeatureSpec()
    market_close = panel.close_adj[MARKET_ID]

    frames: dict[str, pd.DataFrame] = {}
    frames.update(decline.compute(panel, spec, market_close=market_close))
    frames.update(consolidation.compute(panel, spec))
    frames.update(awakening.compute(panel, spec, market_close=market_close))
    frames.update(confirmation.compute(panel, spec, market_close=market_close))
    frames.update(context.compute(panel, spec, market_close=market_close))
    return frames, phase_index


def phase_vectors(
    *,
    security_id: UUID = LIFECYCLE_ID,
    bars_available: int = FULL_HISTORY + 500,
) -> dict[str, BatchFeatureResult]:
    """One `BatchFeatureResult` per phase, for the lifecycle security.

    `as_of` differs per phase and nothing else does — which is what makes
    classifying the sequence a genuine exercise of the dual-mode
    interface rather than six unrelated calls.
    """
    frames, phase_index = phase_frames()
    results: dict[str, BatchFeatureResult] = {}

    for phase in PHASE_ORDER:
        row = phase_index[phase]
        features: dict[str, float | None] = {}
        unavailable: list[str] = []
        for name in FEATURE_NAMES:
            frame = frames.get(name)
            value = frame.iloc[row][security_id] if frame is not None else None
            if value is None or (isinstance(value, float | np.floating) and np.isnan(value)):
                features[name] = None
                unavailable.append(name)
            else:
                features[name] = float(value)

        results[phase] = BatchFeatureResult(
            as_of=_as_of_for(phase),
            feature_schema_version_id=None,
            timeframe=CanonicalTimeframe.DAILY,
            vectors={
                security_id: FeatureVector(
                    security_id=security_id,
                    as_of=_as_of_for(phase),
                    feature_schema_version_id=None,
                    timeframe=CanonicalTimeframe.DAILY,
                    event_time=_as_of_for(phase),
                    availability_time=_as_of_for(phase),
                    features=features,
                    evidence=FeatureEvidence(
                        bars_available=bars_available,
                        bars_required=FULL_HISTORY,
                        missing_inputs={
                            "sector_benchmark": MissReason.NEVER_INGESTED,
                            "industry_benchmark": MissReason.NEVER_INGESTED,
                        },
                        unavailable_features=tuple(unavailable),
                    ),
                )
            },
            missing_securities={},
        )
    return results


def _as_of_for(phase: str) -> datetime:
    """A distinct, ordered `as_of` per phase.

    Spaced a month apart so durations in `market_state_transitions` are
    meaningfully non-zero — a fixture where every phase shared a timestamp
    would make the duration column untestable.
    """
    return datetime(2024, 1 + PHASE_ORDER.index(phase), 1, tzinfo=UTC)
