"""The stored news-volume-anomaly reading, one row per security per day (Module 28).

## What this table is, and what it deliberately is not

A daily batch counts today's `canonical_news` rows against a trailing
baseline and decides whether volume is unusually high — see
`core/news_signals/`. This table is where that decision is written down,
so that reading it is a `SELECT`, never a recomputation. `services/
intelligence` reads this table directly; it does not import the module
that computes it.

It is not a second copy of anything canonical. `canonical_news` remains
the only record of which articles exist; this table records a derived
*reading* about that record's shape on one day, the same relationship
`market_state` has to `market_state_transitions` — except this projection
is per-day rather than "current only", because "was volume unusual on
2026-03-09" has to stay answerable after 2026-03-10 arrives.

## Upsertable, not append-only

`(security_id, signal_date)` is unique, and a rerun of the same day
overwrites that day's row rather than duplicating it — the same
`ON CONFLICT DO UPDATE` shape `core/market_state/transitions.py` uses for
the `market_state` projection. That is deliberate: a same-day rerun after
a partial failure should converge on one correct answer for that day, not
accumulate several. It is *not* append-only guarded for the same reason
migration 0009 gave `live_scan_runs` and 0014 gave `deep_refresh_log`:
this is an operational, recomputable annotation, not a fact ARGUS is
claiming to have believed at a point in time in the sense Module 03's
guards protect.

Past days are not expected to be touched again in practice — the batch
only ever computes "today" — but nothing here prevents a deliberate
backfill from doing so, which is the right default for a table nobody has
asked to make immutable.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from infra.db.metadata import metadata

news_volume_signals = Table(
    "news_volume_signals",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # The trading/calendar date this reading is about, not when the batch
    # happened to run. See core/news_signals/orchestrator.py.
    Column("signal_date", Date, nullable=False),
    # The tri-state verdict. NULL means undetermined — insufficient
    # baseline history — and must never be read as "not raised". A
    # nullable boolean is what makes that distinction representable at
    # the schema level rather than only in application code.
    Column("raised", Boolean, nullable=True),
    Column("today_count", Integer, nullable=False),
    # NULL exactly when `raised` is NULL: there is no reliable baseline to
    # report either.
    Column("baseline_mean", Numeric, nullable=True),
    Column("baseline_window_days", Integer, nullable=False),
    Column("multiple_threshold", Numeric, nullable=False),
    # A `core.data_validation.result.MissReason` value, set exactly when
    # `raised IS NULL`. Text rather than a native enum: this table's
    # vocabulary is a subset reused from a Python enum that several
    # unrelated modules already share, and giving it a second Postgres
    # enum type would mean two places to add a value to in step.
    Column("unavailable_reason", Text, nullable=True),
    Column("config_version_label", Text, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Full reproducibility: the raw counts and window bounds the verdict
    # was computed from, so a stored row can be checked without re-running
    # the batch. Mirrors `RiskFlag.detail`'s reasoning exactly.
    Column("detail", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint("security_id", "signal_date", name="uq_news_volume_signal_security_date"),
    Index("ix_news_volume_signals_security", "security_id", "signal_date"),
    comment=(
        "One stored news-volume-anomaly reading per security per day (Module 28). "
        "raised is tri-state; NULL means undetermined, never 'not raised'."
    ),
)
