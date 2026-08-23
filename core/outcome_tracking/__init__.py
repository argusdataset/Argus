"""Module 15 — outcome tracking and CASE records.

Turns a concluded setup into a permanent, honest record of what happened,
including — especially — when ARGUS was wrong. See
`core/outcome_tracking/README.md`.
"""

from core.outcome_tracking.case_record import (
    AWAKENING_METRICS,
    CONSOLIDATION_METRICS,
    CONTEXT_METRICS,
    DECLINE_METRICS,
    CaseRecord,
    SameAssetHistory,
    StageChecklist,
    build_stage_checklist,
    select_metrics,
)
from core.outcome_tracking.classification import (
    COINCIDENT,
    INFERRED,
    INVALIDATION_EVENTS,
    WEAK,
    ClassificationInputs,
    OutcomeClassification,
    classify,
)
from core.outcome_tracking.config import (
    CALIBRATABLE,
    STRUCTURAL,
    SUCCESS_DEFINITION,
    OutcomeConfig,
    OutcomeThreshold,
    OutcomeThresholds,
    publish_outcome_snapshot,
)
from core.outcome_tracking.engine import (
    OutcomeReport,
    SetupNotConcluded,
    compute_case,
    process_concluded_setups,
    record_outcome,
)
from core.outcome_tracking.excursion import (
    HORIZON,
    TERMINAL_EVENT,
    Excursion,
    OutcomeWindow,
    horizon_end,
    measure,
    outcome_window,
)

__all__ = [
    "AWAKENING_METRICS",
    "CALIBRATABLE",
    "COINCIDENT",
    "CONSOLIDATION_METRICS",
    "CONTEXT_METRICS",
    "DECLINE_METRICS",
    "HORIZON",
    "INFERRED",
    "INVALIDATION_EVENTS",
    "STRUCTURAL",
    "SUCCESS_DEFINITION",
    "TERMINAL_EVENT",
    "WEAK",
    "CaseRecord",
    "ClassificationInputs",
    "Excursion",
    "OutcomeClassification",
    "OutcomeConfig",
    "OutcomeReport",
    "OutcomeThreshold",
    "OutcomeThresholds",
    "OutcomeWindow",
    "SameAssetHistory",
    "SetupNotConcluded",
    "StageChecklist",
    "build_stage_checklist",
    "classify",
    "compute_case",
    "horizon_end",
    "measure",
    "outcome_window",
    "process_concluded_setups",
    "publish_outcome_snapshot",
    "record_outcome",
    "select_metrics",
]
