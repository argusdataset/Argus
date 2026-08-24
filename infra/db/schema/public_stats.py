"""The public page's two tables: what may be published, and what is published.

## `public_release_windows` — the answer to Module 20's open question

Module 17 gates *historical* results: a validation run's numbers reach the
public page only after a named human moves `historical_scan_status` to
APPROVED. Module 18 deliberately does **not** gate live scans, because a
scan succeeding is the system working rather than a result under review.

That left a question nobody had answered: when a setup opened by the live
scanner reaches an outcome, may that outcome be published?

**This table is the answer: yes, but only inside an approved window.**
The reasoning is in `services/public_stats/releases.py`; the shape is
here.

A window is a date range plus the `data_snapshot_id` that labelled its
outcomes — because "which setups concluded between these dates" and
"under which success criterion" are two different questions and both have
to be pinned for a published number to be reproducible.

Rows are **status assignments**, not windows: one row per decision, with
`sequence_number` monotonic per `(period_start, period_end)`. Current
status of a window is its highest sequence. That is `historical_scan_status`'s
shape deliberately, including the lesson migration 0008 taught — an
explicit counter rather than a timestamp, because `now()` is transaction
start time and two decisions in one transaction would order arbitrarily.

## `public_stat_snapshots` — the materialization

Chart payloads, precomputed. The public page is the one surface expected
to take real traffic with no login, and recomputing four aggregates over
every published outcome per request is the wrong shape for that.

The column that makes materialization safe rather than dangerous is
`gate_fingerprint`. A snapshot records exactly which approved runs and
windows it drew from. If a run is later REJECTED, the fingerprint no
longer matches and the snapshot is refused rather than served — see
`services/public_stats/snapshots.py`. Without it, materialization would
mean a withdrawn result staying published until somebody remembered to
refresh, which is precisely the quiet inflation this whole gate exists to
prevent.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
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

from infra.db.enums import HistoricalScanStatus
from infra.db.metadata import metadata, pg_enum

public_release_windows = Table(
    "public_release_windows",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    # The window of live-tracked outcomes this decision covers. Dates
    # rather than timestamps: a reviewer approves "March", not "March plus
    # five hours of session-close offset".
    Column("period_start", Date, nullable=False),
    Column("period_end", Date, nullable=False),
    # Which labelling. Module 15's success criterion is an admitted
    # placeholder, so an outcome's meaning depends on the snapshot that
    # produced it — publishing a window without pinning it would make the
    # published number unreproducible the first time the criterion moves.
    Column(
        "data_snapshot_id",
        UUID(as_uuid=True),
        ForeignKey("data_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("sequence_number", Integer, nullable=False),
    # Same three states as Module 17's gate, deliberately: a reviewer
    # moving live results through the same lifecycle they already know.
    Column(
        "status",
        pg_enum(HistoricalScanStatus, "historical_scan_status_enum"),
        nullable=False,
    ),
    # Nullable only for the system-set PENDING_REVIEW row, exactly as in
    # `historical_scan_status`. Every APPROVED or REJECTED row carries a
    # person — enforced in the service, because an approval nobody's name
    # is on is not a human review.
    Column("assigned_by_user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=True),
    Column("assigned_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("note", Text, nullable=True),
    UniqueConstraint(
        "period_start", "period_end", "sequence_number", name="uq_release_window_sequence"
    ),
    CheckConstraint("period_end >= period_start", name="window_ordered"),
    CheckConstraint("sequence_number >= 0", name="sequence_non_negative"),
    Index("ix_release_window_period", "period_start", "period_end", "sequence_number"),
    comment="Append-only review gate for publishing live-tracked outcomes (Module 20).",
)

public_stat_snapshots = Table(
    "public_stat_snapshots",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    # Which chart. One row per chart per refresh, so a single chart can be
    # served with one indexed lookup.
    Column("chart", Text, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # The PIT cutoff the underlying dataset was loaded at. Served to the
    # public so a number on the page can be tied to an instant.
    Column("as_of", DateTime(timezone=True), nullable=False),
    # Exactly which approved runs and windows this drew from, and a hash
    # of that set. Both: the hash makes the staleness check one
    # comparison, and the list makes a mismatch explainable rather than
    # merely detectable.
    Column("gate_fingerprint", Text, nullable=False),
    Column("included_runs", JSONB, nullable=False, server_default="[]"),
    Column("included_windows", JSONB, nullable=False, server_default="[]"),
    # The chart-ready payload, exactly as served.
    Column("payload", JSONB, nullable=False),
    Column("sample_size", Integer, nullable=False, server_default="0"),
    CheckConstraint("sample_size >= 0", name="sample_size_non_negative"),
    Index("ix_stat_snapshot_chart", "chart", "computed_at"),
    comment="Materialized public chart payloads, with the gate state they were computed under.",
)
