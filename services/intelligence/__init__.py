"""Intelligence API (Module 21): what ARGUS currently thinks, and why.

The module the Intelligence Core has been building toward. Modules 08
through 17 produced evidence; this is where a person finally sees it —
which means this module's job is almost entirely to **surface what already
exists correctly**, not to compute, summarise or interpret anything new.

Three boundaries hold it there, each with a structural test:

- Nothing here computes a score, a state, a similarity or a risk.
- Nothing here imports from `services/public_stats/` — aggregate
  track-record claims are gated, per-security current evidence is not,
  and the two must not meet.
- Nothing here reuses Module 19's User Watchlist shapes. A person's list
  and a derived view of `market_state` are different objects.
"""

from services.intelligence.app import create_app
from services.intelligence.blocks import (
    build_explanation,
    build_freshness,
    build_risk,
    build_score,
    build_similarity,
    build_state,
    watchlists_for_state,
)
from services.intelligence.cases import CLASSIFICATION_CAVEAT, read_case
from services.intelligence.config import (
    CALIBRATABLE,
    KINDS,
    OPERATIONAL,
    STRUCTURAL,
    IntelligenceConfig,
    IntelligenceSetting,
    IntelligenceSettings,
)
from services.intelligence.detail import read_detail, resolve_identity
from services.intelligence.errors import IntelligenceError
from services.intelligence.overlays import BARS_ENDPOINT, read_overlays
from services.intelligence.reads import (
    latest_signal,
    latest_similarity,
    pending_events,
    signal_as_dict,
    state_row,
    transitions_for,
)
from services.intelligence.watchlists import WATCHLIST_NAMES, read_watchlist

__all__ = [
    "BARS_ENDPOINT",
    "CALIBRATABLE",
    "CLASSIFICATION_CAVEAT",
    "KINDS",
    "OPERATIONAL",
    "STRUCTURAL",
    "WATCHLIST_NAMES",
    "IntelligenceConfig",
    "IntelligenceError",
    "IntelligenceSetting",
    "IntelligenceSettings",
    "build_explanation",
    "build_freshness",
    "build_risk",
    "build_score",
    "build_similarity",
    "build_state",
    "create_app",
    "latest_signal",
    "latest_similarity",
    "pending_events",
    "read_case",
    "read_detail",
    "read_overlays",
    "read_watchlist",
    "resolve_identity",
    "signal_as_dict",
    "state_row",
    "transitions_for",
    "watchlists_for_state",
]
