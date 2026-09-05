"""One daily run: assess and store both ownership signals for the universe.

Shaped exactly like `core/news_signals/orchestrator.py`, and for the same
reasons — which is the point rather than a coincidence:

- **Which day** comes from `scan_date_for(now)`, Module 18's own schedule
  function. A fifth independent answer to "which trading day is this"
  would be a fifth place for it to be wrong; asking the one function
  everything else asks makes this module agree with the scanner, the
  ingestion cron, the alert dispatch and Module 28 by construction.
- **Who to compute for** is every member of the named universe version,
  via `list_universe_members_as_of`. Deliberately not filtered to a
  watchlist: this module has no import of `core.market_state` at all, so
  it could not ask "who is on BREAKOUT_READY" even if it wanted to.
- **No FMP client, no `get_config()`.** Both signals read tables Module
  26's ingestion already filled. Nothing here resolves a secret beyond
  the database connection.

## Two signals, one run, two cadences

The insider cluster is per-day and the 13F trend is per-quarter, and they
are computed together anyway. Recomputing the quarterly one daily is
close to free — it reads at most two rows per security and upserts one —
and the alternative, a second schedule that fires four times a year, is a
second thing to notice has stopped working. A daily rerun of a quarter
that has not changed converges on the identical row.

## Why an empty universe is healthy and an unwritten one is not

A run that found members and stored nothing for them has failed at its
one job. A run over an empty universe has not — the same definition
`NewsSignalRunReport` uses, kept identical so the two crons cannot mean
different things by the same word.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine

from core.data_validation.universe import list_universe_members_as_of
from core.live_scanner.schedule import as_of_for, scan_date_for
from core.ownership_signals.config import OwnershipSignalConfig
from core.ownership_signals.insider import assess_insider_batch, store_insider_signals
from core.ownership_signals.institutional import (
    assess_institutional_batch,
    store_institutional_signals,
)
from infra.observability.logging import get_logger

__all__ = ["OwnershipSignalRunReport", "run_daily_ownership_signals"]

_log = get_logger("argus.ownership_signals.run")


@dataclass(slots=True)
class OwnershipSignalRunReport:
    """What one run found and wrote, per signal."""

    scan_date: date | None
    universe_size: int = 0
    insider_raised: int = 0
    insider_not_raised: int = 0
    insider_undetermined: int = 0
    insider_stored: int = 0
    #: 13F rows written. Lower than `universe_size` in normal operation
    #: rather than exceptionally: a security with no 13F data at all has
    #: no quarter to key a row on and is skipped — see
    #: `store_institutional_signals`.
    institutional_stored: int = 0
    institutional_undetermined: int = 0
    skipped_reason: str | None = None

    @property
    def healthy(self) -> bool:
        """A run with nothing due is healthy; one that found members and
        stored no insider readings for them is not.

        Judged on the insider signal alone, deliberately. It writes one
        row per member every run, so zero is unambiguous evidence of
        failure. The 13F count legitimately reads zero for a universe
        whose 13F data has not been ingested yet, and treating that as
        unhealthy would make the cron cry wolf from its first run until
        the first quarterly fetch lands.
        """
        if self.scan_date is None:
            return True
        return self.universe_size == 0 or self.insider_stored > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scan_date": self.scan_date.isoformat() if self.scan_date else None,
            "universe_size": self.universe_size,
            "insider_raised": self.insider_raised,
            "insider_not_raised": self.insider_not_raised,
            "insider_undetermined": self.insider_undetermined,
            "insider_stored": self.insider_stored,
            "institutional_stored": self.institutional_stored,
            "institutional_undetermined": self.institutional_undetermined,
            "skipped_reason": self.skipped_reason,
            "healthy": self.healthy,
        }


def run_daily_ownership_signals(
    engine: Engine,
    *,
    universe_version_id: UUID,
    config: OwnershipSignalConfig | None = None,
    now: datetime | None = None,
) -> OwnershipSignalRunReport:
    """One scheduled wake-up: assess and store today's ownership readings."""
    resolved = config or OwnershipSignalConfig()
    moment = now or datetime.now(UTC)

    scan_date = scan_date_for(moment)
    if scan_date is None:
        report = OwnershipSignalRunReport(
            scan_date=None,
            skipped_reason=(
                "No trading session is due yet. Module 18's schedule answers which "
                "session should have been handled by now, and on a weekend the "
                "answer is none."
            ),
        )
        _log.info(
            "no session due, nothing to assess",
            extra={"event": "ownership_signals_skipped", **report.as_dict()},
        )
        return report

    as_of = as_of_for(scan_date)

    with engine.begin() as connection:
        members = list_universe_members_as_of(connection, universe_version_id, as_of)
        security_ids = [member.security_id for member in members]

        insider = assess_insider_batch(
            connection, security_ids, scan_date=scan_date, as_of=as_of, config=resolved
        )
        insider_stored = store_insider_signals(connection, list(insider.values()))

        institutional = assess_institutional_batch(
            connection, security_ids, as_of=as_of, config=resolved
        )
        institutional_stored = store_institutional_signals(connection, list(institutional.values()))

    report = OwnershipSignalRunReport(
        scan_date=scan_date,
        universe_size=len(security_ids),
        insider_raised=sum(1 for signal in insider.values() if signal.raised is True),
        insider_not_raised=sum(1 for signal in insider.values() if signal.raised is False),
        insider_undetermined=sum(1 for signal in insider.values() if signal.raised is None),
        insider_stored=insider_stored,
        institutional_stored=institutional_stored,
        institutional_undetermined=sum(
            1 for trend in institutional.values() if trend.unavailable is not None
        ),
    )
    _log.info(
        "ownership signal run finished",
        extra={
            "event": "ownership_signals_finished",
            "config_version_label": resolved.version_label(),
            **report.as_dict(),
        },
    )
    return report
