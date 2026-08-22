"""Eligibility gates — can this candidate be judged at all?"""

from core.candidate_detection.eligibility.analogues import (
    AnalogueCount,
    AnalogueCounter,
    PeerProfileAnalogueCounter,
)
from core.candidate_detection.eligibility.bankruptcy import (
    DistressSignals,
    evaluate_bankruptcy_gate,
    load_distress_signals,
)
from core.candidate_detection.eligibility.checks import (
    check_data_history,
    check_data_quality,
    check_liquidity,
    check_valid_asset_identity,
)
from core.candidate_detection.eligibility.gates import (
    EligibilityOutcome,
    EligibilityReport,
    GateResult,
)
from core.candidate_detection.eligibility.runner import evaluate_eligibility, new_run_id

__all__ = [
    "AnalogueCount",
    "AnalogueCounter",
    "DistressSignals",
    "EligibilityOutcome",
    "EligibilityReport",
    "GateResult",
    "PeerProfileAnalogueCounter",
    "check_data_history",
    "check_data_quality",
    "check_liquidity",
    "check_valid_asset_identity",
    "evaluate_bankruptcy_gate",
    "evaluate_eligibility",
    "load_distress_signals",
    "new_run_id",
]
