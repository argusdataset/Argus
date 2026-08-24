"""Public Stats (Module 20): ARGUS's track record, checkable by anyone.

The only module in the project that is public with no login, and the only
place where *"don't manufacture demand, create undeniable value"* becomes
a fact somebody can verify rather than a stated intention.

Its central constraint is a boundary, not a feature: nothing derived from
an unapproved result can reach any endpoint here. That is enforced by
structure — `gate.py` is the single chokepoint, it is the only file
permitted to load outcomes, and a structural test asserts the rest of the
module cannot.
"""

from services.public_stats.aggregates import (
    CHART_CUMULATIVE,
    CHART_EXCURSIONS,
    CHART_REGIME,
    CHART_WIN_RATE,
    CHARTS,
    build_chart,
)
from services.public_stats.app import create_app
from services.public_stats.config import (
    CALIBRATABLE,
    KINDS,
    OPERATIONAL,
    STRUCTURAL,
    PublicStatsConfig,
    PublicStatsSetting,
    PublicStatsSettings,
)
from services.public_stats.errors import PublicStatsError
from services.public_stats.gate import PublishScope, current_scope, published_dataset
from services.public_stats.releases import (
    ReleaseWindow,
    approve_window,
    approved_windows,
    current_window_status,
    open_window_for_review,
    reject_window,
    window_history,
)
from services.public_stats.snapshots import StoredChart, read_chart, refresh_public_stats

__all__ = [
    "CALIBRATABLE",
    "CHARTS",
    "CHART_CUMULATIVE",
    "CHART_EXCURSIONS",
    "CHART_REGIME",
    "CHART_WIN_RATE",
    "KINDS",
    "OPERATIONAL",
    "STRUCTURAL",
    "PublicStatsConfig",
    "PublicStatsError",
    "PublicStatsSetting",
    "PublicStatsSettings",
    "PublishScope",
    "ReleaseWindow",
    "StoredChart",
    "approve_window",
    "approved_windows",
    "build_chart",
    "create_app",
    "current_scope",
    "current_window_status",
    "open_window_for_review",
    "published_dataset",
    "read_chart",
    "refresh_public_stats",
    "reject_window",
    "window_history",
]
