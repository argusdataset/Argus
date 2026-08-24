"""Was there a scan on this date? — the Terminal's only touch on scan output.

## Why this exists at all, and what it deliberately is not

The Terminal shows a user a company today. To say "data as of X" honestly
it has to know whether ARGUS actually ran. That is operational freshness
metadata, and it is the one place this module touches Module 18's output.

It is **not** intelligence. There is no per-security signal here, no
score, no ranking, no watchlist. Those are Module 21's, and serving even
a read-only slice of them from the Terminal is the scope creep the
project's User-vs-Intelligence watchlist boundary exists to prevent.

## Two rules carried forward from Module 18's report, both binding

**A date is not a cutoff.** Rows are stamped with `as_of` — session close
plus an offset — while a caller asks for a calendar date. Reconstructing
that offset here would mean this module owning a copy of Module 18's
scheduling arithmetic, and the copy would drift the first time the offset
changed. So the lookup goes through `scan_results`, which resolves the
date through `live_scan_runs` where both are recorded.

**`available=False` and "available but empty" are different answers.** A
date nobody scanned is not the same as a scanned date that found nothing.
Today the second is the *normal* outcome — Module 13 scores nothing until
the historical case dataset exists — so a Terminal that flattened them
into "no data" would report a correctly-working system as a broken one,
every single day, for as long as it is working correctly.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.engine import Connection

from core.live_scanner.results import scan_results
from services.terminal.schemas import ScanStatusResponse

__all__ = ["read_scan_status"]

_NOT_SCANNED = (
    "No scan has completed for this date. This is not the same as a scan that found "
    "nothing — nobody has looked."
)
_SCANNED_EMPTY = (
    "A scan completed and produced no scored signals. This is currently the expected "
    "outcome: scoring requires a historical case dataset that Module 17's full scan "
    "has not yet produced."
)
_SCANNED_WITH_RESULTS = "A scan completed and produced results."


def read_scan_status(connection: Connection, scan_date: date) -> ScanStatusResponse:
    """Whether ARGUS scanned `scan_date`, and the shape of what it saw.

    Delegates entirely to Module 18's `scan_results`, which is that
    module's intended read surface. No query here duplicates it — in
    particular nothing here reconstructs an `as_of` from a date.
    """
    results = scan_results(connection, scan_date)

    if not results.available:
        return ScanStatusResponse(
            scan_date=scan_date,
            available=False,
            status=results.run.status.value if results.run else None,
            explanation=_NOT_SCANNED,
        )

    explanation = _SCANNED_EMPTY if results.scored_signals == 0 else _SCANNED_WITH_RESULTS
    return ScanStatusResponse(
        scan_date=scan_date,
        available=True,
        status=results.run.status.value if results.run else None,
        scored_signals=results.scored_signals,
        setups_opened=results.setups_opened,
        excluded_count=results.run.excluded_count if results.run else 0,
        explanation=explanation,
    )
