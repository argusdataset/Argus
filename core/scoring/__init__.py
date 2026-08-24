"""Module 13 — the scoring engine.

Combines Modules 08, 10, 11 and 12 into five numbers, or honestly into
none. See `core/scoring/README.md`.
"""

from core.scoring.components import (
    COMPONENT_NAMES,
    DUPLICATE_FEATURES,
    Component,
    ScoringInputs,
    compute_components,
    risk_level,
)
from core.scoring.confidence import (
    CONFIDENCE_FACTORS,
    ConfidenceAssessment,
    ConfidenceFactor,
    compute_confidence,
)
from core.scoring.config import (
    BRIEF_SHARES,
    PROBABILITY_DEFINITION,
    PROBABILITY_STATUS,
    UNASSIGNED_SHARE,
    ConfidenceWeights,
    Normalizers,
    Ramp,
    ScoringConfig,
    ScoringThresholds,
    ScoringWeights,
    publish_scoring_configuration,
    stored_probability_definition,
)
from core.scoring.engine import (
    Lineage,
    ScoredSignal,
    decision_counts,
    score_candidate,
    score_candidates,
)
from core.scoring.gating import (
    GateVerdict,
    ScoringDecision,
    post_scoring_gates,
    pre_scoring_gates,
    weight_coverage,
)
from core.scoring.persistence import (
    SignalNotPersistable,
    resolve_signal_id,
    write_signal,
    write_signals,
)

__all__ = [
    "BRIEF_SHARES",
    "COMPONENT_NAMES",
    "CONFIDENCE_FACTORS",
    "DUPLICATE_FEATURES",
    "PROBABILITY_DEFINITION",
    "PROBABILITY_STATUS",
    "UNASSIGNED_SHARE",
    "Component",
    "ConfidenceAssessment",
    "ConfidenceFactor",
    "ConfidenceWeights",
    "GateVerdict",
    "Lineage",
    "Normalizers",
    "Ramp",
    "ScoredSignal",
    "ScoringConfig",
    "ScoringDecision",
    "ScoringInputs",
    "ScoringThresholds",
    "ScoringWeights",
    "SignalNotPersistable",
    "compute_components",
    "compute_confidence",
    "decision_counts",
    "post_scoring_gates",
    "pre_scoring_gates",
    "publish_scoring_configuration",
    "risk_level",
    "score_candidate",
    "score_candidates",
    "stored_probability_definition",
    "weight_coverage",
    "resolve_signal_id",
    "write_signal",
    "write_signals",
]
