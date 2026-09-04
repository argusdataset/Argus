"""One daily run: which day, who to compute for, and what happened.

## Which day, and why it is not decided here

`scan_date_for(now)` — Module 18's own schedule function, the same one
Modules 26 and 27 already reuse. A fourth independent answer to "which
trading day is this" would be a fourth place for that question to be
wrong; asking the one function everything else asks makes this module
agree with the scanner and the ingestion cron by construction, without
this module needing to know anything about market hours itself.

## Who to compute for

Every member of the named universe version, via `list_universe_members_as_of`
— the same reader Module 26's ingestion reuses rather than re-deriving
membership. Deliberately **not** filtered to a watchlist: this module has
no import of `core.market_state` at all (a structural test asserts it),
so it cannot ask "who is on BREAKOUT_READY" even if it wanted to, and
computing for the whole universe is what lets `services/intelligence`
annotate *any* security's detail view, not only the four watchlists.

## No FMP client, no `get_config()`

Unlike Module 26's ingestion, this run touches no provider and resolves
no secret beyond the database connection itself — `canonical_news` is
already being filled by Module 26's own deep refresh. That also means
this process does not carry `docs/architecture/KNOWN_ISSUES.md` G3's
exposure: nothing here calls `get_config()`, so the ordering hazard that
crashes the ingestion cron on a bare `DATABASE_URL` does not apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine

from core.data_validation.universe import list_universe_members_as_of
from core.live_scanner.schedule import as_of_for, scan_date_for
from core.news_signals.batch import assess_batch, store_signals
from core.news_signals.config import NewsSignalConfig
from infra.observability.logging import get_logger

__all__ = ["NewsSignalRunReport", "run_daily_news_signals"]

_log = get_logger("argus.news_signals.run")


@dataclass(slots=True)
class NewsSignalRunReport:
    """What one run found and wrote."""

    scan_date: date | None
    universe_size: int = 0
    raised: int = 0
    not_raised: int = 0
    undetermined: int = 0
    stored: int = 0
    skipped_reason: str | None = None

    @property
    def healthy(self) -> bool:
        """A run with nothing due is healthy; one that found members and
        stored nothing for them is not."""
        if self.scan_date is None:
            return True
        return self.universe_size == 0 or self.stored > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scan_date": self.scan_date.isoformat() if self.scan_date else None,
            "universe_size": self.universe_size,
            "raised": self.raised,
            "not_raised": self.not_raised,
            "undetermined": self.undetermined,
            "stored": self.stored,
            "skipped_reason": self.skipped_reason,
            "healthy": self.healthy,
        }


def run_daily_news_signals(
    engine: Engine,
    *,
    universe_version_id: UUID,
    config: NewsSignalConfig | None = None,
    now: datetime | None = None,
) -> NewsSignalRunReport:
    """One scheduled wake-up: assess and store today's reading for the universe."""
    resolved = config or NewsSignalConfig()
    moment = now or datetime.now(UTC)

    scan_date = scan_date_for(moment)
    if scan_date is None:
        report = NewsSignalRunReport(
            scan_date=None,
            skipped_reason=(
                "No trading session is due yet. Module 18's schedule answers which "
                "session should have been handled by now, and on a weekend the "
                "answer is none."
            ),
        )
        _log.info(
            "no session due, nothing to assess",
            extra={"event": "news_signals_skipped", **report.as_dict()},
        )
        return report

    as_of = as_of_for(scan_date)

    with engine.begin() as connection:
        members = list_universe_members_as_of(connection, universe_version_id, as_of)
        security_ids = [member.security_id for member in members]

        signals = assess_batch(
            connection,
            security_ids,
            scan_date=scan_date,
            as_of=as_of,
            config=resolved,
        )
        stored = store_signals(connection, list(signals.values()))

    report = NewsSignalRunReport(
        scan_date=scan_date,
        universe_size=len(security_ids),
        raised=sum(1 for signal in signals.values() if signal.raised is True),
        not_raised=sum(1 for signal in signals.values() if signal.raised is False),
        undetermined=sum(1 for signal in signals.values() if signal.raised is None),
        stored=stored,
    )
    _log.info(
        "news signal run finished",
        extra={
            "event": "news_signals_finished",
            "config_version_label": resolved.version_label(),
            **report.as_dict(),
        },
    )
    return report
