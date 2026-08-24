"""Missed dates, processed in order, exactly once each.

## The bound is the point

"Incremental, never re-scans full history" is an architectural property
(the dual-mode interface makes any date scannable) that has to be kept
operationally (this module must only ever actually scan the current date
plus a bounded catch-up window).

So catch-up is bounded twice, and neither bound is decorative:
`max_catchup_days` limits how far back it will look, and the completed-set
check means a date inside that window that has already succeeded is not
re-run. A scheduling bug can therefore cost at most one bounded catch-up,
not a walk back through fifteen years — and when the bound bites,
`CatchupPlan.truncated` says so rather than silently returning a short
list.

## Order is not cosmetic

Oldest first, always. A setup's lifecycle is a sequence of events with
monotonic sequence numbers per setup, and Module 15's outcome windows walk
forward from an activation. Scanning Thursday before Wednesday would
advance those state machines out of order — producing, for instance, an
ACTIVATED event dated before the DETECTED event that should have preceded
it.

## Stopping on failure

`run_catchup` stops at the first date that does not succeed, rather than
skipping it and carrying on. Same reason: the dates are a sequence, and
scanning Friday on top of a Wednesday that never ran produces a Friday
computed against a state that never existed. A stopped catch-up leaves the
remaining dates outstanding, which is recoverable; a catch-up with a hole
in it is not.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from core.live_scanner.config import ScannerConfig
from core.live_scanner.runs import completed_dates
from core.live_scanner.scanner import ConnectionFactory, ScanOutcome, run_scan
from core.live_scanner.schedule import pending_trading_days
from core.model_validation_evaluation.validation.config import ValidationConfig
from core.model_validation_evaluation.validation.replay import ModuleConfigs
from core.scoring.engine import Lineage

__all__ = ["CatchupPlan", "CatchupReport", "plan_catchup", "run_catchup"]


@dataclass(frozen=True, slots=True)
class CatchupPlan:
    """Which dates need scanning, in the order they must run."""

    dates: list[date]
    window_start: date
    window_end: date
    already_complete: int
    #: The oldest trading day in the window is itself outstanding, which
    #: means the scanner has been down longer than the window and dates
    #: older than it have been lost — the bound will never reach them.
    #: False once the scanner is keeping up, which is what makes it worth
    #: reporting rather than a constant.
    truncated: bool

    def __len__(self) -> int:
        return len(self.dates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dates": [day.isoformat() for day in self.dates],
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "already_complete": self.already_complete,
            "truncated": self.truncated,
            "count": len(self.dates),
        }

    def summary(self) -> str:
        if not self.dates:
            return (
                f"Nothing outstanding between {self.window_start} and {self.window_end} "
                f"({self.already_complete} trading day(s) already complete)."
            )
        note = (
            f" The oldest day in the window ({self.dates[0]}) is still outstanding, so "
            "the scanner has been down longer than the catch-up window and older dates "
            "will not be reached — that bound is deliberate (it is what stops a "
            "scheduling mistake re-scanning history), so recovering them is an operator "
            "decision, not something catch-up should do on its own."
            if self.truncated
            else ""
        )
        return (
            f"{len(self.dates)} date(s) outstanding, {self.dates[0]} to {self.dates[-1]}, "
            f"to be scanned oldest first.{note}"
        )


@dataclass(frozen=True, slots=True)
class CatchupReport:
    """What a catch-up run actually did."""

    plan: CatchupPlan
    outcomes: list[ScanOutcome] = field(default_factory=list)
    stopped_at: date | None = None
    stop_reason: str = ""

    @property
    def scanned(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.succeeded and not outcome.skipped)

    @property
    def complete(self) -> bool:
        return self.stopped_at is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.as_dict(),
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "scanned": self.scanned,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "stop_reason": self.stop_reason,
            "complete": self.complete,
        }


def plan_catchup(
    connect: ConnectionFactory,
    *,
    now: datetime,
    config: ScannerConfig | None = None,
) -> CatchupPlan:
    """The outstanding trading days, oldest first.

    Reads the completed set from `live_scan_runs` rather than inferring
    completion from whether signals exist for a date. A day on which
    nothing scored is a perfectly normal successful scan — today, it is
    the *expected* one — so "are there signals" would re-run every quiet
    day forever.
    """
    config = config or ScannerConfig()
    window = config.settings.catchup_window
    window_start = _as_date(now) - timedelta(days=window)
    window_end = _as_date(now)

    with connect() as connection:
        done = completed_dates(connection, since=window_start, until=window_end)

    outstanding = pending_trading_days(now, completed=done, config=config)
    candidates = pending_trading_days(now, completed=set(), config=config)

    return CatchupPlan(
        dates=outstanding,
        window_start=window_start,
        window_end=window_end,
        already_complete=len(candidates) - len(outstanding),
        # The oldest trading day the window covers is *itself* still
        # outstanding. That is the operationally interesting case: it
        # means the gap is at least as long as the window, so there are
        # almost certainly older dates the bound will never reach.
        # Reported rather than silently absorbed, because "we are caught
        # up" and "we are as caught up as the bound allows" are different
        # claims and only one of them is good news.
        truncated=bool(outstanding) and bool(candidates) and outstanding[0] == candidates[0],
    )


def run_catchup(
    connect: ConnectionFactory,
    *,
    now: datetime,
    lineage: Lineage,
    config: ScannerConfig | None = None,
    validation_config: ValidationConfig | None = None,
    modules: ModuleConfigs | None = None,
    benchmark_security_id: UUID | None = None,
    sleep: Callable[[float], None] | None = None,
) -> CatchupReport:
    """Scan every outstanding date in order, stopping at the first that does not finish.

    Each date goes through `run_scan`, which means each gets the full
    policy — readiness gate, retries, quarantine, its own run record. A
    catch-up is not a lesser scan; it is the same scan for an older `as_of`.
    """
    config = config or ScannerConfig()
    plan = plan_catchup(connect, now=now, config=config)

    outcomes: list[ScanOutcome] = []
    for scan_date in plan.dates:
        outcome = run_scan(
            connect,
            scan_date=scan_date,
            lineage=lineage,
            config=config,
            validation_config=validation_config,
            modules=modules,
            benchmark_security_id=benchmark_security_id,
            sleep=sleep,
        )
        outcomes.append(outcome)
        if not outcome.succeeded:
            return CatchupReport(
                plan=plan,
                outcomes=outcomes,
                stopped_at=scan_date,
                stop_reason=(
                    f"{scan_date.isoformat()} ended {outcome.status.value} and catch-up "
                    "stopped there. The dates are a sequence — scanning a later one on "
                    f"top of a missing {scan_date.isoformat()} would compute it against "
                    "a state that never existed. Reason: " + outcome.reason
                ),
            )

    return CatchupReport(plan=plan, outcomes=outcomes)


def _as_date(moment: datetime) -> date:
    return moment.astimezone(UTC).date() if moment.tzinfo else moment.date()
