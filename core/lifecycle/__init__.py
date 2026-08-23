"""Module 14 — setup qualification and lifecycle.

Event-sourced: a setup's status is always derived from its `setup_events`
history, never stored. See `core/lifecycle/README.md`.
"""

from core.lifecycle.config import (
    CALIBRATABLE,
    STRUCTURAL,
    LifecycleConfig,
    LifecycleThreshold,
    QualificationThresholds,
)
from core.lifecycle.derivation import (
    LIFECYCLE_ORDER,
    LifecycleRegression,
    LifecycleState,
    advance,
    current_status,
    history,
    open_setups,
    state_from,
)
from core.lifecycle.engine import (
    ACTIONS,
    ACTIVATION_STATES,
    DETECTION_STATES,
    ENDPOINT_STATES,
    CandidateObservation,
    LifecycleReport,
    LifecycleResult,
    advance_lifecycle,
    open_setup,
)
from core.lifecycle.events import (
    EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    SetupEvent,
    append_event,
)

__all__ = [
    "ACTIONS",
    "ACTIVATION_STATES",
    "CALIBRATABLE",
    "DETECTION_STATES",
    "ENDPOINT_STATES",
    "EVENT_TYPES",
    "LIFECYCLE_ORDER",
    "STRUCTURAL",
    "TERMINAL_EVENT_TYPES",
    "CandidateObservation",
    "LifecycleConfig",
    "LifecycleRegression",
    "LifecycleReport",
    "LifecycleResult",
    "LifecycleState",
    "LifecycleThreshold",
    "QualificationThresholds",
    "SetupEvent",
    "advance",
    "advance_lifecycle",
    "append_event",
    "current_status",
    "history",
    "open_setup",
    "open_setups",
    "state_from",
]
