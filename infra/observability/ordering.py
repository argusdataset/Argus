"""The "latest row wins" audit Module 17 asked someone to spend an hour on.

Module 17's words:

> "Worth stating because the same trap exists anywhere else a table means
> 'the latest row wins' — I did not audit for other instances, and that
> audit is probably worth someone's hour."

This file is that hour, written down so it does not have to be spent
again. It is a registry rather than prose: every place ARGUS derives a
current value from row ordering, what makes it safe or not, and — for the
ones that are not — a probe that detects the ambiguity actually occurring
in a live database.

## The trap, restated

Migration 0008's lesson, in three parts:

1. A timestamp column defaulting to `now()` gets **transaction start
   time**, not statement time. Two rows written in one transaction carry
   the *same* value.
2. The usual tiebreak is the primary key, which is `gen_random_uuid()` —
   **random**.
3. So "the latest row" for that parent is decided by a coin flip, and a
   different coin flip on the next read.

All three must hold. Break any one and the ordering is safe: an explicit
monotonic counter breaks (1), a caller-supplied timestamp breaks (1), a
uniqueness constraint that makes two same-parent rows impossible in one
transaction breaks (2), and an ordering used only for display breaks (3)
in the sense that nothing depends on the answer.

## What was found

Nothing new was fixed. Two genuine hazards were found, both narrow,
neither fixable inside this module's boundary — fixing either means
changing what Modules 15/17/11/21 or Module 20 *decide*, which is
business logic this module is told not to touch. So they are registered
here, and `ordering_report` probes for them actually happening, which
converts a latent ambiguity into an observable one. That is the trade an
observability module should make: it is not this module's place to change
the writer, and it is exactly this module's place to notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Table, func, literal_column, select
from sqlalchemy.engine import Connection

from infra.db.schema.public_stats import public_stat_snapshots
from infra.db.schema.setups import setup_outcomes

__all__ = ["AUDIT", "AmbiguousOrdering", "OrderedRead", "ordering_report", "probe"]


@dataclass(frozen=True, slots=True)
class OrderedRead:
    """One place ARGUS derives a current value from row ordering."""

    table: str
    #: The column the "latest" is taken on.
    ordering: str
    #: Who reads it this way.
    readers: tuple[str, ...]
    #: `True` when the ordering cannot be ambiguous. See `basis`.
    safe: bool
    #: Why it is safe, or why it is not.
    basis: str
    #: The column identifying the parent whose "latest" is being picked.
    parent: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "ordering": self.ordering,
            "parent": self.parent,
            "readers": list(self.readers),
            "safe": self.safe,
            "basis": self.basis,
        }


#: Every ordered read in ARGUS, audited. Enumerable so the next person
#: checks a list rather than repeating the search.
AUDIT: tuple[OrderedRead, ...] = (
    # ---- safe: an explicit monotonic counter ------------------------
    OrderedRead(
        table="historical_scan_status",
        ordering="sequence_number",
        parent="run_id",
        readers=("core.model_validation_evaluation.validation.review",),
        safe=True,
        basis=(
            "Migration 0008 replaced the `assigned_at` ordering with a monotonic "
            "counter, database-enforced unique per run. This is the original bug and "
            "its fix."
        ),
    ),
    OrderedRead(
        table="public_release_windows",
        ordering="sequence_number",
        parent="(period_start, period_end)",
        readers=("services.public_stats.releases",),
        safe=True,
        basis=(
            "Built with the counter from the start (migration 0011) rather than "
            "acquiring one after the bug — Module 20 read 0008's lesson first."
        ),
    ),
    OrderedRead(
        table="setup_events",
        ordering="sequence_number",
        parent="setup_id",
        readers=("core.lifecycle.derivation",),
        safe=True,
        basis=(
            "`current_status` orders by `sequence_number` and nothing else. A setup "
            "legitimately gets several events in one transaction, so this is the table "
            "where the trap would bite hardest — and it is the one that never had it."
        ),
    ),
    OrderedRead(
        table="live_scan_runs",
        ordering="attempt",
        parent="scan_date",
        readers=("core.live_scanner.runs",),
        safe=True,
        basis=(
            "`attempt` is monotonic per scan date and uniquely constrained. The second "
            "instance of the bug pattern, already fixed."
        ),
    ),
    # ---- safe: the timestamp is not a `now()` default ---------------
    OrderedRead(
        table="security_ticker_history",
        ordering="valid_from",
        parent="security_id",
        readers=(
            "services.terminal.company",
            "services.terminal.datafeed",
            "services.intelligence.detail",
            "services.intelligence.watchlists",
            "data.normalization.identity",
        ),
        safe=True,
        basis=(
            "`valid_from` is supplied by the caller, not defaulted to `now()`, and an "
            "exclusion constraint forbids two overlapping rows for one ticker. Two rows "
            "in one transaction cannot share a `valid_from` and both be current."
        ),
    ),
    OrderedRead(
        table="canonical_ohlcv / canonical_fundamentals / canonical_corporate_actions",
        ordering="observation_time, availability_time",
        parent="(security_id, event_time)",
        readers=("data.canonical_model.pit", "core.data_validation.bulk", "services.terminal.bars"),
        safe=True,
        basis=(
            "Both timestamps come from the provider payload and the ingestion clock, "
            "never from a `now()` default, and uniqueness includes `observation_time`. "
            "Two restatements of one bar in one transaction would need identical "
            "observation times, which the constraint rejects."
        ),
    ),
    OrderedRead(
        table="login_attempts",
        ordering="attempted_at",
        parent="email",
        readers=("services.identity.attempts",),
        safe=True,
        basis=(
            "`attempted_at` is written from Python's clock per call, not from the "
            "server-side `now()` default, so rows in one transaction get distinct "
            "values. The lockout also compares against a floor rather than picking a "
            "single latest row, so a tie would not change its answer."
        ),
    ),
    OrderedRead(
        table="registration_attempts",
        ordering="attempted_at",
        parent="ip_address",
        readers=("services.identity.attempts",),
        safe=True,
        basis=(
            "Module 24's own new table, added to this registry when it introduced it "
            "rather than left for the next audit to find. `attempted_at` is written "
            "from Python's clock per call, matching `login_attempts` above — but "
            "`registration_lockout_state` does not even take 'the latest row': it "
            "sums every attempt in the window with `func.count()`, never picks one "
            "and calls it current. There is no winner for a tie to make ambiguous."
        ),
    ),
    # ---- safe: ordering is for display, nothing depends on the winner
    OrderedRead(
        table="market_state",
        ordering="entered_at",
        parent=None,
        readers=("core.market_state.watchlists",),
        safe=True,
        basis=(
            "One row per security by construction, so there is no 'latest of several' "
            "to pick. The ordering sorts a watchlist for a reader."
        ),
    ),
    OrderedRead(
        table="canonical_news / user_watchlists / sessions",
        ordering="event_time / created_at / issued_at",
        parent=None,
        readers=(
            "services.terminal.news",
            "services.terminal.watchlists",
            "services.identity.sessions",
        ),
        safe=True,
        basis=(
            "Listings, not current-value reads. A tie changes the order two items "
            "appear in and nothing else; no decision is taken from the first row."
        ),
    ),
    OrderedRead(
        table="history",
        ordering="valid_from",
        parent="security_id",
        readers=("data.normalization.identity",),
        safe=True,
        basis=(
            "`security_ticker_history` under a local alias. Same reasoning as the row "
            "above; listed under the alias so the completeness scan resolves it."
        ),
    ),
    # ---- NOT safe -----------------------------------------------------
    OrderedRead(
        table="setup_outcomes",
        ordering="recorded_at",
        parent="setup_id",
        readers=(
            "core.historical_similarity.cases",
            "core.model_validation_evaluation.evaluation.dataset",
            "services.intelligence.cases",
        ),
        safe=False,
        basis=(
            "All three parts of the trap hold. `recorded_at` defaults to `now()`; "
            "migration 0007 deliberately permits one outcome per (setup, snapshot), so "
            "two labellings of one setup can be written in one transaction; and the "
            "tiebreak is `o.id DESC`, a random UUID — or, in "
            "`services.intelligence.cases`, no tiebreak at all. Reachable by recording "
            "outcomes under two snapshots in one transaction, which nothing forbids. "
            "The consequence is the serious kind: Module 17's evaluation dataset and "
            "Module 11's case set would each pick a labelling at random, so the same "
            "database could produce two different answers — which is the reproducibility "
            "guarantee, not a cosmetic ordering."
        ),
    ),
    OrderedRead(
        table="signals",
        ordering="event_time, created_at",
        parent="security_id",
        readers=("services.intelligence.reads",),
        safe=False,
        basis=(
            "Found by this module's own completeness scan rather than by reading, "
            "which is the argument for the scan. `latest_signal` takes the newest "
            "signal for a security ordered by `event_time` then `created_at`. "
            "`event_time` is the scan date and is caller-supplied, so it ties "
            "legitimately; `created_at` defaults to `now()`; and there is no further "
            "tiebreak. Migration 0005's unique index is on `(security_id, event_time, "
            "data_snapshot_id, scoring_configuration_id)`, so two signals for one "
            "security on one date under different snapshots or configurations are "
            "permitted — and written in one transaction they are indistinguishable. "
            "Module 21 would then serve an arbitrary one of two scores, and a second "
            "read could serve the other."
        ),
    ),
    OrderedRead(
        table="historical_similarity_results",
        ordering="event_time, computed_at",
        parent="(security_id, scope)",
        readers=("services.intelligence.reads",),
        safe=False,
        basis=(
            "The same shape as `signals`, found the same way. `latest_similarity` "
            "keeps the first row per scope after ordering by `event_time` then "
            "`computed_at`. Uniqueness is `(security_id, event_time, scope, "
            "data_snapshot_id)`, so two results for one security and scope on one "
            "date under different snapshots are permitted; `computed_at` defaults to "
            "`now()` and there is no tiebreak. Lower severity than `signals` — the "
            "block is served with its sufficiency and would be reported honestly "
            "either way — but the choice is still a coin flip."
        ),
    ),
    OrderedRead(
        table="public_stat_snapshots",
        ordering="computed_at",
        parent="chart",
        readers=("services.public_stats.snapshots",),
        safe=False,
        basis=(
            "`computed_at` defaults to `now()`, there is no uniqueness on `chart`, and "
            "there is no tiebreak. Two refreshes of one chart in one transaction are "
            "ambiguous. Low severity: both rows carry the same `gate_fingerprint` and "
            "are verified against the live gate before serving, so the two payloads "
            "would be identical in content. Registered because 'identical today' is not "
            "'identical by construction'."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class AmbiguousOrdering:
    """A parent that actually has two rows sharing an ordering value."""

    table: str
    parent: str
    ordering_value: str
    rows: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "parent": self.parent,
            "ordering_value": self.ordering_value,
            "rows": self.rows,
        }


def probe(connection: Connection) -> list[AmbiguousOrdering]:
    """Look for the ambiguity having actually happened, in this database.

    The registry says where the hazard is; this says whether it has bitten.
    An empty list is the expected and healthy answer, and it is a
    meaningfully different statement from "we checked the code and it
    looked fine".
    """
    found: list[AmbiguousOrdering] = []
    found.extend(
        _duplicates(
            connection,
            table=setup_outcomes,
            parent=setup_outcomes.c.setup_id,
            ordering=setup_outcomes.c.recorded_at,
            label="setup_outcomes",
        )
    )
    found.extend(
        _duplicates(
            connection,
            table=public_stat_snapshots,
            parent=public_stat_snapshots.c.chart,
            ordering=public_stat_snapshots.c.computed_at,
            label="public_stat_snapshots",
        )
    )
    return found


def _duplicates(
    connection: Connection, *, table: Table, parent: Any, ordering: Any, label: str
) -> list[AmbiguousOrdering]:
    rows = connection.execute(
        select(parent.label("parent"), ordering.label("value"), func.count().label("rows"))
        .select_from(table)
        .group_by(parent, ordering)
        .having(func.count() > literal_column("1"))
    ).all()
    return [
        AmbiguousOrdering(
            table=label,
            parent=str(row.parent),
            ordering_value=str(row.value),
            rows=int(row.rows),
        )
        for row in rows
    ]


def ordering_report(connection: Connection | None = None) -> dict[str, Any]:
    """The audit, plus — when given a connection — whether it has bitten."""
    unsafe = [entry for entry in AUDIT if not entry.safe]
    report: dict[str, Any] = {
        "as_of": datetime.now(UTC).isoformat(),
        "reads_audited": len(AUDIT),
        "unsafe": [entry.as_dict() for entry in unsafe],
        "safe": [entry.as_dict() for entry in AUDIT if entry.safe],
    }
    if connection is not None:
        observed = probe(connection)
        report["observed_ambiguities"] = [item.as_dict() for item in observed]
        report["healthy"] = not observed
    return report
