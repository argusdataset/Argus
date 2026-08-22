"""Feature Engineering Engine — built in Module 08.

Computes the numeric description of a security's price/volume/volatility
behaviour that every downstream module reads and never recomputes.

    from core.feature_engine import compute_features_batch, FeatureSpec

    result = compute_features_batch(connection, security_ids, as_of, spec=spec)

`as_of` is a plain argument with no live/batch distinction — a live scan
and a Module 17 historical replay are the same call with a different
value. Batch is the real implementation; the single-security
`compute_features` delegates to it.
"""

from core.feature_engine.engine import compute_features, compute_features_batch
from core.feature_engine.panel import PricePanel, load_panel
from core.feature_engine.persistence import write_feature_vectors
from core.feature_engine.spec import (
    FEATURE_GROUPS,
    FEATURE_NAMES,
    FeatureSpec,
    FeatureTolerances,
    FeatureWindows,
    publish_feature_schema_version,
)
from core.feature_engine.timeframes import (
    DERIVABLE_TIMEFRAMES,
    UnsupportedTimeframeError,
    derive_panel,
)
from core.feature_engine.vector import (
    BatchFeatureResult,
    FeatureEvidence,
    FeatureVector,
)

__all__ = [
    "DERIVABLE_TIMEFRAMES",
    "FEATURE_GROUPS",
    "FEATURE_NAMES",
    "BatchFeatureResult",
    "FeatureEvidence",
    "FeatureSpec",
    "FeatureTolerances",
    "FeatureVector",
    "FeatureWindows",
    "PricePanel",
    "UnsupportedTimeframeError",
    "compute_features",
    "compute_features_batch",
    "derive_panel",
    "load_panel",
    "publish_feature_schema_version",
    "write_feature_vectors",
]
