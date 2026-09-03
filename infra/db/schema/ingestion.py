"""The deep-refresh log (Module 26).

## What this table is for, and what it deliberately is not

Module 26 refreshes fundamentals and news at a frequency set by a
security's current watchlist phase — 30 days in DOWN_TREND, 10 in
CONSOLIDATION, daily in BREAKOUT_READY and UPTREND. To decide whether a
security is due it needs one fact it cannot derive: **when did the last
completed refresh happen, and which phase drove it.**

That is what this table holds, and the shape is a *log* rather than a
per-security row that gets overwritten. The difference matters:

- A mutable `current_phase` column would be a second stored copy of
  "what phase is this security in", and `core/market_state/watchlists.py`
  is explicit that a watchlist is a filter over the `market_state`
  projection and never an independently stored value. The moment the two
  disagreed there would be no way to say which was right.
- What this table stores instead is `triggering_watchlist` — the phase
  that drove *one completed refresh*, which is a historical fact about
  that event and stays true forever. Nothing reads it as an answer to
  "what phase is this security in now"; that is always read live.

## Why `refreshed_on` is a date

The tier intervals are declared in days, and the ingestion job runs once
per trading day. Comparing dates rather than wall-clock instants means a
cron firing that drifts ninety seconds early cannot skip a daily tier by
landing at 23h 58m since the last one. `refreshed_at` keeps the exact
instant for anyone investigating a run.

`(security_id, refreshed_on)` is unique, which is also what makes a
same-day re-run a no-op at the database level rather than only in a
checkpoint file — see `core/ingestion/README.md` on why the filesystem
cannot be the durable answer on Railway.

## Not append-only guarded

Same reasoning migration 0009 applied to `live_scan_runs`: Module 03's
guards exist for rows whose editing would rewrite what ARGUS believed at
a point in time. A refresh record is an operational fact about a job, and
nothing downstream computes a statistic from it.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from infra.db.enums import MarketState
from infra.db.metadata import metadata, pg_enum

deep_refresh_log = Table(
    "deep_refresh_log",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # The ingestion run's target trading date. The unit due-ness is
    # decided in; see the module docstring.
    Column("refreshed_on", Date, nullable=False),
    Column("refreshed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # The tier key the decision actually used. Recorded rather than
    # re-derived because the state -> watchlist map is code, and code
    # changes; this says what this run decided on.
    Column("triggering_watchlist", Text, nullable=False),
    # The observed state behind that tier key — the finer fact, kept
    # because "CONSOLIDATION the watchlist" covers two states.
    Column("market_state", pg_enum(MarketState, "market_state_enum"), nullable=False),
    # never_refreshed | interval_elapsed | tier_escalation. Which of the
    # two due-ness rules fired, so a run's decisions are explicable
    # after the fact rather than only reproducible.
    Column("trigger", Text, nullable=False),
    Column("statements_written", Integer, nullable=False, server_default="0"),
    Column("news_written", Integer, nullable=False, server_default="0"),
    # IngestionConfig.version_label(), so a row says which tier policy
    # was in force when it was written.
    Column("config_version_label", Text, nullable=False),
    Column("detail", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint("security_id", "refreshed_on", name="uq_deep_refresh_security_date"),
    Index("ix_deep_refresh_latest", "security_id", "refreshed_on"),
    comment=(
        "One completed tiered deep refresh of one security (Module 26). "
        "triggering_watchlist is a historical fact about this refresh, never "
        "an authoritative 'current phase'."
    ),
)
