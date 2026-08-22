"""Market State Engine (Module 10)."""

from core.market_state.classifier import (
    ClassificationResult,
    StateAssignment,
    classify_states,
)
from core.market_state.states import (
    CLASSIFICATION_FEATURES,
    INTERNAL_STATES,
    STATE_DEFINITIONS,
    WATCHLISTS,
    StateDefinition,
    watchlist_for,
)
from core.market_state.target_model_matching.interface import (
    TargetModel,
    TargetModelAssessment,
)
from core.market_state.thresholds import (
    MarketStateConfig,
    StateThresholds,
    Threshold,
    publish_target_model_version,
)
from core.market_state.transitions import (
    CYCLE_ORDER,
    StoredState,
    Transition,
    backward_transition_count,
    cycle_count,
    history,
    load_current_states,
    record_transitions,
)
from core.market_state.watchlists import WATCHLIST_NAMES, all_watchlists, watchlist

__all__ = [
    "CLASSIFICATION_FEATURES",
    "CYCLE_ORDER",
    "INTERNAL_STATES",
    "STATE_DEFINITIONS",
    "WATCHLISTS",
    "WATCHLIST_NAMES",
    "ClassificationResult",
    "MarketStateConfig",
    "StateAssignment",
    "StateDefinition",
    "StateThresholds",
    "StoredState",
    "TargetModel",
    "TargetModelAssessment",
    "Threshold",
    "Transition",
    "all_watchlists",
    "backward_transition_count",
    "classify_states",
    "cycle_count",
    "history",
    "load_current_states",
    "publish_target_model_version",
    "record_transitions",
    "watchlist",
    "watchlist_for",
]
