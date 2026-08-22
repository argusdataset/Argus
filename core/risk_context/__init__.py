"""Module 12 — risk / context analysis and pending material events.

Deliberately thin. See `core/risk_context/README.md` for what was left
out on purpose and why.
"""

from core.risk_context.assessment import RiskContext, assess_risk_context
from core.risk_context.config import RiskConfig, RiskThreshold, RiskThresholds
from core.risk_context.events import (
    EARNINGS,
    EventCoverage,
    MaterialEventRecord,
    PendingEvent,
    PendingEventsView,
    pending_events_as_of,
    translate_earnings_event,
    write_material_events,
)
from core.risk_context.flags import (
    EVENT_PROXIMITY,
    FLAG_NAMES,
    LIQUIDITY_DEGREE,
    VOLATILITY_SPIKE,
    RiskFlag,
)
from core.risk_context.invalidation import (
    EligibilityChange,
    EligibilityTrend,
    InvalidationSignals,
    assess_invalidation,
    eligibility_history,
)

__all__ = [
    "EARNINGS",
    "EVENT_PROXIMITY",
    "FLAG_NAMES",
    "LIQUIDITY_DEGREE",
    "VOLATILITY_SPIKE",
    "EligibilityChange",
    "EligibilityTrend",
    "EventCoverage",
    "InvalidationSignals",
    "MaterialEventRecord",
    "PendingEvent",
    "PendingEventsView",
    "RiskConfig",
    "RiskContext",
    "RiskFlag",
    "RiskThreshold",
    "RiskThresholds",
    "assess_invalidation",
    "assess_risk_context",
    "eligibility_history",
    "pending_events_as_of",
    "translate_earnings_event",
    "write_material_events",
]
