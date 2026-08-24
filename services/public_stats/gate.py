"""The review-gate boundary. Every published number passes through this file.

## The structure, and why it is a structure rather than a rule

The requirement is that nothing derived from a `PENDING_REVIEW` or
`REJECTED` result can reach the public page. A rule like that, enforced
by everybody remembering to add a `WHERE`, fails the first time someone
adds an endpoint in a hurry.

So it is enforced by shape instead:

- **This is the only file in `services/public_stats/` that may load
  setups or outcomes.** A structural test asserts that — no other module
  here imports `load_evaluation_dataset`, names the `setups` or
  `setup_outcomes` tables, or imports Module 17's review functions.
- **Loading is impossible without a scope.** `published_dataset` takes no
  period, no universe, no snapshot. It derives all of them from
  `approved_runs()` and `approved_windows()`, so there is no argument a
  caller could pass to widen it.
- **An empty gate yields an empty dataset, never an unfiltered one.**
  The failure mode of a filter built from a list is that an empty list
  means "no filter". `PublishScope.is_empty` is checked before any query
  runs, and `published_dataset` returns an empty frame rather than
  falling through to a load with no bounds.

## What "derived from an approved run" means, concretely

Module 17's `model_validation_runs` records a period, a universe version
and a data snapshot. Its own evaluation loads exactly that window with
exactly that universe (`evaluate()` in `evaluation/engine.py`), so a run's
results *are* the setups in that window under that universe. This module
uses the same three bounds, through the same loader, so the public
numbers are the same numbers the evaluation produced rather than a second
computation that could disagree.

Live outcomes come from `approved_windows()` — see `releases.py` for the
decision that they need approving at all.

## Deduplication is not a tidiness concern

A setup can fall inside an approved historical run's period *and* an
approved live window — a replay of recent history overlapping the live
period does exactly that. Counted twice, it inflates every published
statistic, and inflates it in ARGUS's favour whenever the setup
succeeded. The union is deduplicated on `setup_id` before any aggregate
sees it.

## The fingerprint

`PublishScope.fingerprint` hashes the exact set of runs and windows the
scope covers. `snapshots.py` stores it with a materialized payload and
refuses to serve one whose fingerprint no longer matches. Without it,
materializing would mean a withdrawn result staying on the page until
somebody remembered to refresh.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pandas as pd

from core.model_validation_evaluation.evaluation.dataset import (
    EvaluationDataset,
    load_evaluation_dataset,
)
from core.model_validation_evaluation.validation.review import approved_runs
from core.model_validation_evaluation.validation.runs import load_run
from services.public_stats.releases import approved_windows

__all__ = [
    "ApprovedRunScope",
    "ApprovedWindowScope",
    "PublishScope",
    "current_scope",
    "published_dataset",
]


@dataclass(frozen=True, slots=True)
class ApprovedRunScope:
    """One approved validation run's bounds."""

    run_id: UUID
    period_start: datetime
    period_end: datetime
    universe_version_id: UUID
    data_snapshot_id: UUID

    def as_dict(self) -> dict[str, str]:
        return {
            "run_id": str(self.run_id),
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ApprovedWindowScope:
    """One approved live-release window's bounds."""

    period_start: datetime
    period_end: datetime
    data_snapshot_id: UUID

    def as_dict(self) -> dict[str, str]:
        return {
            "period_start": self.period_start.date().isoformat(),
            "period_end": self.period_end.date().isoformat(),
            "data_snapshot_id": str(self.data_snapshot_id),
        }


@dataclass(frozen=True, slots=True)
class PublishScope:
    """Everything ARGUS is currently allowed to publish. Nothing else exists.

    Built only by `current_scope`. There is no constructor argument that
    adds a period, and no method that widens one — a caller holding a
    scope holds exactly what the two gates permitted at the moment it was
    built.
    """

    runs: tuple[ApprovedRunScope, ...] = ()
    windows: tuple[ApprovedWindowScope, ...] = ()
    built_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_empty(self) -> bool:
        return not self.runs and not self.windows

    def fingerprint(self) -> str:
        """A hash of exactly what this scope covers.

        Sorted, so the same approved set always hashes the same regardless
        of query order. Includes the snapshot each window publishes under,
        because re-approving a window against a different labelling is a
        different publication even though the dates match.
        """
        parts = sorted(str(run.run_id) for run in self.runs)
        parts += sorted(
            f"{w.period_start.date()}|{w.period_end.date()}|{w.data_snapshot_id}"
            for w in self.windows
        )
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": [run.as_dict() for run in self.runs],
            "windows": [window.as_dict() for window in self.windows],
            "fingerprint": self.fingerprint(),
            "built_at": self.built_at.isoformat(),
        }

    def covers(self, other: PublishScope) -> bool:
        """Whether `other`'s contents are all still inside this scope.

        The distinction that decides whether a stored snapshot is merely
        *incomplete* or has been *withdrawn*: an approval that adds a run
        leaves every previously-published number still true, while a
        rejection that removes one does not.
        """
        run_ids = {run.run_id for run in self.runs}
        window_keys = {
            (w.period_start.date(), w.period_end.date(), w.data_snapshot_id) for w in self.windows
        }
        return all(run.run_id in run_ids for run in other.runs) and all(
            (w.period_start.date(), w.period_end.date(), w.data_snapshot_id) in window_keys
            for w in other.windows
        )


def current_scope(connection) -> PublishScope:
    """What ARGUS may publish right now.

    Two calls, both of which are the only door their respective module
    offers: Module 17's `approved_runs` and this module's
    `approved_windows`. Neither takes a parameter that relaxes it, and
    nothing here filters their output further — a run that is approved is
    in scope, and one that is not cannot be.
    """
    runs: list[ApprovedRunScope] = []
    for run_id in approved_runs(connection):
        run = load_run(connection, run_id)
        if run is None:  # pragma: no cover - a FK makes this unreachable
            continue
        runs.append(
            ApprovedRunScope(
                run_id=run.id,
                period_start=run.period_start,
                period_end=run.period_end,
                universe_version_id=run.lineage.universe_version_id,
                data_snapshot_id=run.lineage.data_snapshot_id,
            )
        )

    windows = [
        ApprovedWindowScope(
            period_start=datetime.combine(window.period_start, datetime.min.time(), tzinfo=UTC),
            period_end=datetime.combine(window.period_end, datetime.max.time(), tzinfo=UTC),
            data_snapshot_id=window.data_snapshot_id,
        )
        for window in approved_windows(connection)
    ]

    return PublishScope(runs=tuple(runs), windows=tuple(windows))


def published_dataset(
    connection, scope: PublishScope, *, as_of: datetime | None = None
) -> EvaluationDataset:
    """Every outcome ARGUS may publish, as one dataset. Nothing else.

    Loaded through Module 17's own `load_evaluation_dataset`, once per
    approved run and once per approved window, then concatenated and
    deduplicated on `setup_id`.

    Per-scope rather than one query, deliberately: each approved run has
    its own universe version and its own snapshot, and a single query
    spanning them would have to either drop those bounds or reconstruct
    them — and dropping them is how a setup from an unapproved universe
    ends up in a published average. The number of approved scopes is
    small (a handful of reviewed runs, a window per review cadence), and
    this runs at refresh time rather than per request.
    """
    moment = as_of or datetime.now(UTC)

    # An empty gate yields an empty dataset. Falling through to a load
    # with no bounds is the specific bug this early return exists to make
    # impossible: a filter built from an empty list is not a filter.
    if scope.is_empty:
        return _empty(moment)

    frames: list[pd.DataFrame] = []
    for run in scope.runs:
        frames.append(
            load_evaluation_dataset(
                connection,
                as_of=moment,
                period_start=run.period_start,
                period_end=run.period_end,
                universe_version_id=run.universe_version_id,
            ).frame
        )
    for window in scope.windows:
        frames.append(
            load_evaluation_dataset(
                connection,
                as_of=moment,
                period_start=window.period_start,
                period_end=window.period_end,
                data_snapshot_id=window.data_snapshot_id,
            ).frame
        )

    present = [frame for frame in frames if not frame.empty]
    if not present:
        return _empty(moment)

    combined = pd.concat(present, ignore_index=True)
    # A setup inside both an approved run's period and an approved live
    # window would otherwise be counted twice — and counted twice in
    # ARGUS's favour whenever it succeeded.
    combined = combined.drop_duplicates(subset=["setup_id"], keep="first")

    return EvaluationDataset(frame=combined.reset_index(drop=True), as_of=moment)


def _empty(as_of: datetime) -> EvaluationDataset:
    from core.model_validation_evaluation.evaluation.dataset import EVALUATION_COLUMNS

    return EvaluationDataset(frame=pd.DataFrame(columns=list(EVALUATION_COLUMNS)), as_of=as_of)
