"""Target model matching — contained sub-components of the state engine."""

from core.market_state.target_model_matching.interface import (
    TargetModel,
    TargetModelAssessment,
    assessment_evidence,
)

__all__ = ["TargetModel", "TargetModelAssessment", "assessment_evidence"]
