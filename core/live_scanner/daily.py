"""The entry point a scheduler calls: one function, one decision tree, no arguments about time.

## What a scheduler is and is not responsible for

Something outside ARGUS has to wake this process up — cron, a systemd
timer, a container scheduler, a Module 23 supervisor. What that thing must
*not* do is decide which date to scan. If cron says "run at 21:05 ET" and
the process assumes that means today, then a run that fires late, a run
that fires twice, a holiday, and a clock skew all become correctness bugs
in the scheduler's configuration rather than in code anyone tests.

So `run_daily(now=...)` takes the current instant and works out the rest:
which trading session is due, whether it has already been scanned, whether
anything older is outstanding. The scheduler's only job is to call this
often enough. Calling it every thirty minutes and calling it once at 21:05
both produce a correct day; the first just recovers faster from a late
feed.

That also means the retry interval is not something the scheduler has to
know. A DATA_NOT_READY result is not an error and does not need special
handling — the next wake-up asks again, and the readiness window is what
eventually stops it.

## Catch-up is not a separate mode

`run_daily` runs catch-up. There is no "normal path" that scans today and
a "recovery path" somebody remembers to run after an outage — the outage
is exactly when nobody remembers. Today's date is simply the last entry in
the outstanding list, and on a healthy day the list has one entry in it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from core.live_scanner.catchup import CatchupReport, run_catchup
from core.live_scanner.config import ScannerConfig
from core.live_scanner.scanner import ConnectionFactory
from core.model_validation_evaluation.validation.config import ValidationConfig
from core.model_validation_evaluation.validation.replay import ModuleConfigs
from core.scoring.engine import Lineage

__all__ = ["DailyReport", "run_daily"]


@dataclass(frozen=True, slots=True)
class DailyReport:
    """One wake-up: what was outstanding and what happened to it."""

    now: datetime
    catchup: CatchupReport

    @property
    def scanned(self) -> int:
        return self.catchup.scanned

    @property
    def healthy(self) -> bool:
        """Whether this wake-up left the scanner in a good state.

        A wake-up with nothing to do is healthy. A wake-up that hit
        DATA_NOT_READY is healthy — the data is late, the next wake-up
        asks again. A wake-up that stopped on a FAILED date is not.
        """
        return self.catchup.complete or all(
            outcome.status.value == "DATA_NOT_READY" for outcome in self.catchup.outcomes[-1:]
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "now": self.now.isoformat(),
            "scanned": self.scanned,
            "healthy": self.healthy,
            "catchup": self.catchup.as_dict(),
        }

    def summary(self) -> str:
        lines = [
            f"Live scanner wake-up at {self.now.isoformat()}.",
            f"  {self.catchup.plan.summary()}",
        ]
        for outcome in self.catchup.outcomes:
            marker = "ok" if outcome.succeeded else outcome.status.value
            lines.append(f"  {outcome.scan_date} [{marker}] {outcome.reason}")
        if self.catchup.stopped_at is not None:
            lines.append(f"  STOPPED at {self.catchup.stopped_at}: {self.catchup.stop_reason}")
        return "\n".join(lines)


def run_daily(
    connect: ConnectionFactory,
    *,
    lineage: Lineage,
    now: datetime | None = None,
    config: ScannerConfig | None = None,
    validation_config: ValidationConfig | None = None,
    modules: ModuleConfigs | None = None,
    benchmark_security_id: UUID | None = None,
    sleep: Callable[[float], None] | None = None,
) -> DailyReport:
    """Scan whatever is outstanding, oldest first. Safe to call repeatedly.

    `now` defaults to the wall clock and is the **only** place in this
    module's call graph that reads one. Everything downstream derives its
    `as_of` from a session close, which is what makes a scan of a given
    date produce the same answer whenever it happens to run — and what
    makes this function testable at any instant a test cares to name.
    """
    moment = now or datetime.now(UTC)
    report = run_catchup(
        connect,
        now=moment,
        lineage=lineage,
        config=config,
        validation_config=validation_config,
        modules=modules,
        benchmark_security_id=benchmark_security_id,
        sleep=sleep,
    )
    return DailyReport(now=moment, catchup=report)
