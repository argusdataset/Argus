"""Module 28 — Reactive news-volume signal.

Answers one narrow question, per security, per day: did unusually many
`canonical_news` articles show up today, relative to this security's own
trailing average? Not a prediction and not a read of *what* the news
says — a volume anomaly only, computed once a day and stored, so
`services/intelligence` can annotate a security's detail view and its
watchlist entries with it by reading a row rather than recomputing
anything.

Deliberately outside `core/risk_context/`: that module's flags feed
Module 13's `risk_level` and this signal must never join them — see
`docs/architecture/KNOWN_ISSUES.md` and the boundary tests in
`tests/unit/news_signals/`. `core/market_state/`, `core/scoring/`,
`core/candidate_detection/eligibility/` and `core/live_scanner/` must
never import this package, and a structural test asserts none of them
do.

See README.md for the full reasoning, the daily process, and what a
"raised" reading is worth.
"""

from core.news_signals.config import NewsSignalConfig, NewsSignalThreshold, NewsSignalThresholds
from core.news_signals.orchestrator import NewsSignalRunReport, run_daily_news_signals
from core.news_signals.signal import NewsVolumeSignal, evaluate

__all__ = [
    "NewsSignalConfig",
    "NewsSignalRunReport",
    "NewsSignalThreshold",
    "NewsSignalThresholds",
    "NewsVolumeSignal",
    "evaluate",
    "run_daily_news_signals",
]
