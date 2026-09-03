"""Module 26 — Daily Ingestion Orchestration + Tiered Deep Refresh.

Decides *when* to fetch *what* for *whom*, and calls Module 04 to fetch
and Module 05 to normalize and persist. It does neither itself.

See README.md for the two scopes, the FMP-endpoint decision, the request
volume, and the one number outside this module that currently stops the
scanner from acting on what this writes.
"""

from core.ingestion.config import (
    COST_POLICY,
    COST_POLICY_BASIS,
    INGESTION_KINDS,
    TIER_SETTINGS,
    IngestionConfig,
    IngestionSetting,
    IngestionSettings,
)
from core.ingestion.deep_refresh import DeepRefreshReport, refresh_due_securities
from core.ingestion.members import MemberSet, UniverseMember, universe_members
from core.ingestion.orchestrator import IngestionReport, run_daily_ingestion
from core.ingestion.phases import current_phases, watchlist_for_state
from core.ingestion.prices import OhlcvReport, ingest_daily_prices
from core.ingestion.refresh_log import CompletedRefresh, last_refreshes, record_refresh
from core.ingestion.strategy import EndpointStrategy, StrategyDecision, select_strategy
from core.ingestion.tiers import DueDecision, RefreshRecord, decide

__all__ = [
    "COST_POLICY",
    "COST_POLICY_BASIS",
    "INGESTION_KINDS",
    "TIER_SETTINGS",
    "CompletedRefresh",
    "DeepRefreshReport",
    "DueDecision",
    "EndpointStrategy",
    "IngestionConfig",
    "IngestionReport",
    "IngestionSetting",
    "IngestionSettings",
    "MemberSet",
    "OhlcvReport",
    "RefreshRecord",
    "StrategyDecision",
    "UniverseMember",
    "current_phases",
    "decide",
    "ingest_daily_prices",
    "last_refreshes",
    "record_refresh",
    "refresh_due_securities",
    "run_daily_ingestion",
    "select_strategy",
    "universe_members",
    "watchlist_for_state",
]
