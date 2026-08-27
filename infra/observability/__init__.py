"""Observability (Module 23). Making twenty-two modules' discipline visible.

Nothing here computes a new fact. Every signal is something an earlier
module already decided and recorded — Module 10's `unclassified_reason`,
Module 18's `LiveScanStatus`, Module 16's verifier verdicts, the PIT
columns Module 03 made non-nullable — read and counted rather than
re-derived.
"""

from infra.observability.anomalies import (
    NO_PREDICATE_MATCHED,
    RenderTally,
    StateGapReport,
    observed_renderer,
    state_gaps,
    unclassified_now,
)
from infra.observability.config import ObservabilityConfig, ObservabilitySettings
from infra.observability.freshness import (
    FEEDS,
    FeedFreshness,
    FreshnessState,
    all_feeds,
    feed_freshness,
)
from infra.observability.health import Check, HealthReport, Status, check_health
from infra.observability.logging import (
    CREDENTIAL_KEY_PARTS,
    REDACTED,
    JsonFormatter,
    configure_logging,
    fields,
    get_logger,
    logging_is_configured,
    reset_logging,
    scrub,
)
from infra.observability.ordering import AUDIT, OrderedRead, ordering_report, probe
from infra.observability.pipeline import (
    IngestionLag,
    ScanHealth,
    ingestion_health,
    scan_health,
    stuck_runs,
)

__all__ = [
    "AUDIT",
    "CREDENTIAL_KEY_PARTS",
    "FEEDS",
    "NO_PREDICATE_MATCHED",
    "REDACTED",
    "Check",
    "FeedFreshness",
    "FreshnessState",
    "HealthReport",
    "IngestionLag",
    "JsonFormatter",
    "ObservabilityConfig",
    "ObservabilitySettings",
    "OrderedRead",
    "RenderTally",
    "ScanHealth",
    "StateGapReport",
    "Status",
    "all_feeds",
    "check_health",
    "configure_logging",
    "feed_freshness",
    "fields",
    "get_logger",
    "ingestion_health",
    "logging_is_configured",
    "observed_renderer",
    "ordering_report",
    "probe",
    "reset_logging",
    "scan_health",
    "scrub",
    "state_gaps",
    "stuck_runs",
    "unclassified_now",
]
