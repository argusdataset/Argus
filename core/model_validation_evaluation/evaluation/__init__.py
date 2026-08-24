"""Model Evaluation (Module 17): does it work, and under what conditions."""

from core.model_validation_evaluation.evaluation.breakdowns import (
    CROSS_ASSET_ONLY,
    SAME_ASSET_SUPPORTED,
    Breakdown,
    by_evidence_scope,
    by_regime,
    by_regime_and_bucket,
    sufficiency_note,
)
from core.model_validation_evaluation.evaluation.buckets import (
    CalibrationBin,
    CalibrationCurve,
    MonotonicityAnalysis,
    MonotonicityViolation,
    ScoreBucket,
    analyse_monotonicity,
    build_buckets,
    calibration_curve,
)
from core.model_validation_evaluation.evaluation.config import (
    EvaluationConfig,
    EvaluationThreshold,
    EvaluationThresholds,
    ScoreBucketing,
)
from core.model_validation_evaluation.evaluation.dataset import (
    EVALUATION_COLUMNS,
    EvaluationDataset,
    load_evaluation_dataset,
)
from core.model_validation_evaluation.evaluation.engine import (
    EvaluationRefused,
    EvaluationReport,
    build_report,
    evaluate,
)
from core.model_validation_evaluation.evaluation.metrics import (
    RESOLVED_STATUSES,
    ConfusionMatrix,
    MetricSuite,
    compute_metrics,
    confusion_matrix,
    max_drawdown,
)
from core.model_validation_evaluation.evaluation.walk_forward import (
    Roll,
    WalkForwardAnalysis,
    Window,
    build_windows,
    walk_forward,
)

__all__ = [
    "CROSS_ASSET_ONLY",
    "EVALUATION_COLUMNS",
    "RESOLVED_STATUSES",
    "SAME_ASSET_SUPPORTED",
    "Breakdown",
    "CalibrationBin",
    "CalibrationCurve",
    "ConfusionMatrix",
    "EvaluationConfig",
    "EvaluationDataset",
    "EvaluationRefused",
    "EvaluationReport",
    "EvaluationThreshold",
    "EvaluationThresholds",
    "MetricSuite",
    "MonotonicityAnalysis",
    "MonotonicityViolation",
    "Roll",
    "ScoreBucket",
    "ScoreBucketing",
    "WalkForwardAnalysis",
    "Window",
    "analyse_monotonicity",
    "build_buckets",
    "build_report",
    "build_windows",
    "by_evidence_scope",
    "by_regime",
    "by_regime_and_bucket",
    "calibration_curve",
    "compute_metrics",
    "confusion_matrix",
    "evaluate",
    "load_evaluation_dataset",
    "max_drawdown",
    "sufficiency_note",
    "walk_forward",
]
