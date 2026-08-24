"""Historical Similarity Engine (Module 11)."""

from core.historical_similarity.cases import CaseSet, load_cases
from core.historical_similarity.config import (
    SimilarityConfig,
    SimilarityThreshold,
    SimilarityThresholds,
    publish_similarity_configuration,
)
from core.historical_similarity.counter import (
    RE_DERIVED_MIN_ANALOGUES,
    HistoricalAnalogueCounter,
)
from core.historical_similarity.distance import (
    FeatureContribution,
    FeatureScales,
    estimate_scales,
    explain,
    pairwise_distances,
)
from core.historical_similarity.engine import (
    SameAssetHistory,
    ScopedEvidence,
    SimilarityEvidence,
    SimilarMatch,
    find_similar_setups,
    insufficient,
    load_same_asset_history,
)
from core.historical_similarity.features import (
    DUPLICATE_FEATURE_GROUPS,
    excluded_as_duplicate,
    exclusion_report,
    metric_features,
)
from core.historical_similarity.persistence import write_similarity_results
from core.historical_similarity.statistics import (
    Distribution,
    Interval,
    OutcomeStatistics,
    SampleSufficiency,
    mean_confidence_interval,
    summarize,
    wilson_interval,
)

__all__ = [
    "DUPLICATE_FEATURE_GROUPS",
    "RE_DERIVED_MIN_ANALOGUES",
    "CaseSet",
    "Distribution",
    "FeatureContribution",
    "FeatureScales",
    "HistoricalAnalogueCounter",
    "Interval",
    "OutcomeStatistics",
    "SameAssetHistory",
    "SampleSufficiency",
    "ScopedEvidence",
    "SimilarMatch",
    "SimilarityConfig",
    "SimilarityEvidence",
    "SimilarityThreshold",
    "SimilarityThresholds",
    "estimate_scales",
    "excluded_as_duplicate",
    "exclusion_report",
    "explain",
    "find_similar_setups",
    "insufficient",
    "load_cases",
    "mean_confidence_interval",
    "load_same_asset_history",
    "metric_features",
    "pairwise_distances",
    "publish_similarity_configuration",
    "summarize",
    "wilson_interval",
    "write_similarity_results",
]
