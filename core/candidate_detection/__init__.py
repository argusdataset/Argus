"""Candidate detection and eligibility gating (Module 09)."""

from core.candidate_detection.config import (
    DetectionConfig,
    DetectionParameters,
    EligibilityParameters,
    publish_detection_configuration,
)
from core.candidate_detection.detection import RANKING_COMPONENTS, detect_candidates
from core.candidate_detection.eligibility import (
    AnalogueCount,
    AnalogueCounter,
    EligibilityOutcome,
    EligibilityReport,
    GateResult,
    PeerProfileAnalogueCounter,
    evaluate_eligibility,
    new_run_id,
)
from core.candidate_detection.persistence import write_eligibility_results
from core.candidate_detection.pool import Candidate, CandidatePool, ExclusionReason

__all__ = [
    "RANKING_COMPONENTS",
    "AnalogueCount",
    "AnalogueCounter",
    "Candidate",
    "CandidatePool",
    "DetectionConfig",
    "DetectionParameters",
    "EligibilityOutcome",
    "EligibilityParameters",
    "EligibilityReport",
    "ExclusionReason",
    "GateResult",
    "PeerProfileAnalogueCounter",
    "detect_candidates",
    "evaluate_eligibility",
    "new_run_id",
    "publish_detection_configuration",
    "write_eligibility_results",
]
