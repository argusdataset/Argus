"""The daily scan's own record (Module 18).

## Why this is not `model_validation_runs`

A live scan and a validation run do the same computation and answer
different questions. `model_validation_runs` means "a model was replayed
over history so its results could be evaluated and reviewed"; every row
there is a candidate for Module 17's review gate. A daily scan is not up
for review — it is the system doing its job — and writing 250 rows a year
into the gate's queue would bury the runs that genuinely need a human.

So: a separate table, with the lineage columns every result table in
ARGUS carries, and a status enum that says more than "worked / did not".

## One row per attempt

`(scan_date, attempt)` is unique and `attempt` is monotonic per date. A
retry is a new row rather than an overwrite, so the history of a date that
took four tries is recoverable — which is the question anyone
investigating a flaky scanner actually asks. Within a row, `status` moves
RUNNING -> terminal exactly once, the same mutable-row lifecycle Module 03
gave `model_validation_runs`.

Deliberately *not* append-only-with-a-projection: a scan run is an
operational record, not a claim about the market, and nothing downstream
computes a statistic from it. The append-only guarantee exists for rows
whose editing would rewrite what ARGUS believed at a point in time; this
is not one of those.

## What is not here

No row is written for a non-trading day. A weekend is not a scan that was
skipped, it is a date that was never a scan date, and recording one row
per weekend forever would make the table mostly noise.
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

from infra.db.enums import LiveScanStatus
from infra.db.metadata import metadata, pg_enum

live_scan_runs = Table(
    "live_scan_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    # The trading date scanned. A date, not a timestamp: "did we scan
    # 2026-03-04" is the question catch-up asks, and a timestamp would
    # make two attempts at different clock times look like two dates.
    Column("scan_date", Date, nullable=False),
    # The PIT cutoff the scan actually used. Distinct from `scan_date`
    # because a scan of Tuesday's session runs on Tuesday evening, and
    # every read inside it is bounded by this instant, not by midnight.
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("status", pg_enum(LiveScanStatus, "live_scan_status"), nullable=False),
    # The same six IDs every result table carries. A day's signals are
    # attributable to a configuration through their own rows; this makes
    # the *run* attributable too, which is what lets a later investigation
    # ask "what changed between the day this worked and the day it did
    # not" without joining through the signals.
    Column(
        "target_model_version_id",
        UUID(as_uuid=True),
        ForeignKey("target_model_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "feature_schema_version_id",
        UUID(as_uuid=True),
        ForeignKey("feature_schema_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "scoring_configuration_id",
        UUID(as_uuid=True),
        ForeignKey("scoring_configuration.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "detection_configuration_id",
        UUID(as_uuid=True),
        ForeignKey("detection_configuration.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "universe_version_id",
        UUID(as_uuid=True),
        ForeignKey("universe_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "data_snapshot_id",
        UUID(as_uuid=True),
        ForeignKey("data_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    # Securities quarantined to let the rest of the day complete, with the
    # reason each was excluded. A list rather than a count: "which name
    # has been failing every day this week" is the question that makes
    # per-security isolation worth having at all, and a count cannot
    # answer it.
    Column("excluded_securities", JSONB, nullable=False, server_default="[]"),
    # The scan's own counts (universe size, candidates, signals written,
    # lifecycle actions), plus the retry history and, on a failure, what
    # went wrong. Module 18 owns the shape.
    Column("detail", JSONB, nullable=False, server_default="{}"),
    # Short, human-first summary. The structured story is in `detail`;
    # this is what a Module 23 observability layer would put on a line.
    Column("note", Text, nullable=True),
    UniqueConstraint("scan_date", "attempt", name="uq_live_scan_date_attempt"),
    CheckConstraint("attempt >= 0", name="attempt_non_negative"),
    Index("ix_live_scan_date", "scan_date", "attempt"),
    Index("ix_live_scan_status", "status", "scan_date"),
    comment="One attempt at one trading day's live scan (Module 18).",
)
