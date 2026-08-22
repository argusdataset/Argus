"""The feature engine's entry points.

Two functions, one code path. `compute_features_batch` is the real
implementation; `compute_features` is a single-security convenience that
delegates to it. That direction matters — the batch path is not "the
single-security function in a loop", it is the other way round, which is
the only arrangement that can survive a universe-scale historical scan.

## Why the batch path is genuinely vectorized

Three properties, each checkable:

1. **Two SQL queries regardless of universe size.** One for bars, one for
   corporate actions (`core.data_validation.bulk`), not two per security.
2. **Every feature is computed on wide (dates × securities) frames.**
   `close.rolling(20).mean()` covers ten thousand securities in one call.
   No feature function ever receives a single security's series.
3. **The only per-security Python work is the final slice** — reading one
   row out of the already-computed frames into a `FeatureVector`. That is
   O(universe) cheap dictionary construction, not O(universe) numerical
   work.

`tests/integration/feature_engine/test_batch_performance.py` asserts
property 1 by counting queries, which is the property a disguised loop
cannot fake.

## `as_of` is a plain argument

No live/batch distinction anywhere in the interface, matching Module 07's
`get_as_of`. A live scan passes `datetime.now(UTC)`; Module 17's
historical replay passes a date in 2015. Same function, same arguments,
same return type — which is what
`docs/architecture/CROSS_CUTTING_REQUIREMENTS.md` asks Modules 08-16 to
guarantee.
"""

from __future__ import annotations

import math
from datetime import datetime
from uuid import UUID

import pandas as pd
from sqlalchemy.engine import Connection

from core.data_validation.result import MissReason
from core.feature_engine.groups import awakening, confirmation, consolidation, context, decline
from core.feature_engine.panel import PricePanel, load_panel
from core.feature_engine.spec import FEATURE_NAMES, FeatureSpec
from core.feature_engine.timeframes import derive_panel
from core.feature_engine.vector import (
    BatchFeatureResult,
    FeatureEvidence,
    FeatureVector,
)
from data.canonical_model.records import CanonicalTimeframe


def compute_features_batch(
    connection: Connection,
    security_ids: list[UUID],
    as_of: datetime,
    *,
    spec: FeatureSpec | None = None,
    feature_schema_version_id: UUID | None = None,
    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY,
    market_security_id: UUID | None = None,
    sector_map: dict[UUID, UUID] | None = None,
    industry_map: dict[UUID, UUID] | None = None,
) -> BatchFeatureResult:
    """Compute features for many securities at one `as_of`, in one pass.

    `market_security_id` names the benchmark (SPY) — it is loaded as part
    of the same panel query, so relative strength costs no extra round
    trip. `sector_map` / `industry_map` map each security to a benchmark
    security; both are optional because **ARGUS stores no sector data
    today** (see `groups/context.py`), and their absence produces honest
    NaNs plus a recorded `MissReason`, never a fabricated benchmark.
    """
    spec = spec or FeatureSpec()
    requested = list(dict.fromkeys(security_ids))

    benchmark_ids = _benchmark_ids(market_security_id, sector_map, industry_map)
    panel = load_panel(
        connection,
        requested + [b for b in benchmark_ids if b not in requested],
        as_of,
        max_lookback_bars=spec.windows.max_lookback,
    )

    if panel.is_empty():
        return BatchFeatureResult(
            as_of=as_of,
            feature_schema_version_id=feature_schema_version_id,
            timeframe=timeframe,
            vectors={},
            missing_securities=dict.fromkeys(requested, MissReason.NOT_YET_AVAILABLE),
        )

    panel = derive_panel(panel, timeframe)

    present = [s for s in requested if s in panel.close_adj.columns]
    missing = {
        s: MissReason.NOT_YET_AVAILABLE for s in requested if s not in panel.close_adj.columns
    }

    market_close = _series_for(panel, market_security_id)
    sector_close = _benchmark_frame(panel, sector_map, present)
    industry_close = _benchmark_frame(panel, industry_map, present)

    frames = _compute_all_groups(panel, spec, timeframe, market_close, sector_close, industry_close)
    vectors = _slice_vectors(
        panel=panel,
        frames=frames,
        securities=present,
        as_of=as_of,
        spec=spec,
        timeframe=timeframe,
        feature_schema_version_id=feature_schema_version_id,
        has_market=market_close is not None,
        has_sector=sector_close is not None,
        has_industry=industry_close is not None,
    )

    return BatchFeatureResult(
        as_of=as_of,
        feature_schema_version_id=feature_schema_version_id,
        timeframe=timeframe,
        vectors=vectors,
        missing_securities=missing,
    )


def compute_features(
    connection: Connection,
    security_id: UUID,
    as_of: datetime,
    *,
    spec: FeatureSpec | None = None,
    feature_schema_version_id: UUID | None = None,
    timeframe: CanonicalTimeframe = CanonicalTimeframe.DAILY,
    market_security_id: UUID | None = None,
    sector_map: dict[UUID, UUID] | None = None,
    industry_map: dict[UUID, UUID] | None = None,
) -> FeatureVector:
    """One security, one date — for interactive and debugging use.

    Delegates to the batch path so there is exactly one implementation of
    every feature. A caller computing the whole universe this way would
    pay two queries per security; `compute_features_batch` is the entry
    point Modules 09 and 18 should use.
    """
    result = compute_features_batch(
        connection,
        [security_id],
        as_of,
        spec=spec,
        feature_schema_version_id=feature_schema_version_id,
        timeframe=timeframe,
        market_security_id=market_security_id,
        sector_map=sector_map,
        industry_map=industry_map,
    )
    if security_id in result.vectors:
        return result.vectors[security_id]

    spec = spec or FeatureSpec()
    return FeatureVector(
        security_id=security_id,
        as_of=as_of,
        feature_schema_version_id=feature_schema_version_id,
        timeframe=timeframe,
        event_time=None,
        availability_time=None,
        features=dict.fromkeys(FEATURE_NAMES),
        evidence=FeatureEvidence(
            bars_available=0,
            bars_required=spec.windows.max_lookback,
            missing_inputs={"price_history": MissReason.NOT_YET_AVAILABLE},
            unavailable_features=tuple(FEATURE_NAMES),
        ),
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _compute_all_groups(
    panel: PricePanel,
    spec: FeatureSpec,
    timeframe: CanonicalTimeframe,
    market_close: pd.Series | None,
    sector_close: pd.DataFrame | None,
    industry_close: pd.DataFrame | None,
) -> dict[str, pd.DataFrame]:
    """Every group's features, still as wide frames covering the universe."""
    frames: dict[str, pd.DataFrame] = {}
    frames.update(
        decline.compute(panel, spec, market_close=market_close, sector_close=sector_close)
    )
    frames.update(consolidation.compute(panel, spec, timeframe=timeframe))
    frames.update(
        awakening.compute(panel, spec, market_close=market_close, sector_close=sector_close)
    )
    frames.update(
        confirmation.compute(panel, spec, market_close=market_close, sector_close=sector_close)
    )
    frames.update(
        context.compute(
            panel,
            spec,
            market_close=market_close,
            sector_close=sector_close,
            industry_close=industry_close,
            timeframe=timeframe,
        )
    )
    return frames


def _slice_vectors(
    *,
    panel: PricePanel,
    frames: dict[str, pd.DataFrame],
    securities: list[UUID],
    as_of: datetime,
    spec: FeatureSpec,
    timeframe: CanonicalTimeframe,
    feature_schema_version_id: UUID | None,
    has_market: bool,
    has_sector: bool,
    has_industry: bool,
) -> dict[UUID, FeatureVector]:
    """Read the final row of every feature frame into per-security vectors.

    The only per-security work in the whole module, and it is dictionary
    construction rather than numerical computation — every number was
    already produced by the vectorized pass above.
    """
    if len(panel.dates) == 0:
        return {}

    last_row = {name: frame.iloc[-1] for name, frame in frames.items()}
    event_time = panel.dates[-1]
    bar_counts = panel.bar_counts()

    shared_missing: dict[str, MissReason] = {}
    if not has_market:
        shared_missing["market_benchmark"] = MissReason.NEVER_INGESTED
    if not has_sector:
        shared_missing["sector_benchmark"] = MissReason.NEVER_INGESTED
    if not has_industry:
        shared_missing["industry_benchmark"] = MissReason.NEVER_INGESTED

    vectors: dict[UUID, FeatureVector] = {}
    for security_id in securities:
        features: dict[str, float | None] = {}
        unavailable: list[str] = []
        for name in FEATURE_NAMES:
            series = last_row.get(name)
            value = series.get(security_id) if series is not None else None
            if value is None or (isinstance(value, float) and math.isnan(value)):
                features[name] = None
                unavailable.append(name)
            else:
                features[name] = float(value)

        available_bars = int(bar_counts.get(security_id, 0))
        vectors[security_id] = FeatureVector(
            security_id=security_id,
            as_of=as_of,
            feature_schema_version_id=feature_schema_version_id,
            timeframe=timeframe,
            event_time=event_time.to_pydatetime(),
            availability_time=_availability_for(panel, security_id),
            features=features,
            evidence=FeatureEvidence(
                bars_available=available_bars,
                bars_required=spec.windows.max_lookback,
                missing_inputs=dict(shared_missing),
                unavailable_features=tuple(unavailable),
            ),
        )
    return vectors


def _availability_for(panel: PricePanel, security_id: UUID) -> datetime | None:
    """Max input `availability_time`, per Module 03's definition."""
    if panel.availability is None or len(panel.availability) == 0:
        return None
    value = panel.availability.get(security_id)
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).to_pydatetime()


def _benchmark_ids(
    market_security_id: UUID | None,
    sector_map: dict[UUID, UUID] | None,
    industry_map: dict[UUID, UUID] | None,
) -> list[UUID]:
    """Every benchmark security that must be loaded alongside the universe."""
    ids: list[UUID] = []
    if market_security_id is not None:
        ids.append(market_security_id)
    for mapping in (sector_map, industry_map):
        if mapping:
            ids.extend(mapping.values())
    return list(dict.fromkeys(ids))


def _series_for(panel: PricePanel, security_id: UUID | None) -> pd.Series | None:
    if security_id is None or security_id not in panel.close_adj.columns:
        return None
    return panel.close_adj[security_id]


def _benchmark_frame(
    panel: PricePanel, mapping: dict[UUID, UUID] | None, securities: list[UUID]
) -> pd.DataFrame | None:
    """A per-security benchmark frame, aligned column-for-column.

    Each security's column holds its own sector/industry benchmark's
    prices, so the relative-strength calculation stays a single
    element-wise division across the whole universe.
    """
    if not mapping:
        return None
    columns = {}
    for security_id in securities:
        benchmark_id = mapping.get(security_id)
        if benchmark_id is not None and benchmark_id in panel.close_adj.columns:
            columns[security_id] = panel.close_adj[benchmark_id]
    if not columns:
        return None
    return pd.DataFrame(columns, index=panel.close_adj.index)
