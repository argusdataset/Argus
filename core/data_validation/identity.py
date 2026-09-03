"""What a security was called, as of a moment. One reader, several callers.

## Why this exists rather than a fourth private copy

Nothing in ARGUS references a security by ticker — Module 05's central
rule — so every surface that has to *show* one, or hand one to a
provider, must translate `security_id` into the ticker that identity held
at a given instant. Four places needed that and each wrote its own:

- `services/terminal/company.py::_profile` — one security, ordered by
  `valid_from`, for the company page;
- `services/intelligence/watchlists.py::_tickers` — many securities, in
  one query, for a watchlist page;
- `core/ingestion/members.py::_tickers_for` — many, for the FMP fetch;
- and Module 27 would have been the fourth.

All four apply the same half-open validity predicate to the same table,
and all four are private. Three copies of a rule is a rule that will
eventually be four different rules — Module 15's report already flagged
this shape once, for a different reader, with the same conclusion: it
belongs in Module 07.

So this is the reader, and Module 07 is where the other PIT-bounded
entity readers already live. `core/ingestion/members.py` now calls it.
The two service-layer copies are *not* migrated here: they return
different shapes under other modules' tests, and rewriting them is a
refactor of Modules 19 and 21 rather than part of building an alerts
bot. They are named above so the next person to touch either one knows
where the shared version is.

## The predicate, and the one place it differs from `_profile`

`valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)` — the
same half-open interval Module 05's exclusion constraints enforce, so at
most one row can match per security and "which one" is never a sort
question. `services/terminal/company.py` instead takes the latest row
with `valid_from <= as_of`, which differs only for a security whose
ticker window has closed with nothing opened after it — a delisted name
whose ticker was released. That is a real difference and the reason its
migration is a decision rather than a rename: the company page wants to
say what a delisted security *was* called; a fetch or an alert wants to
know what it is called *now*, and for a released ticker the honest answer
is nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.engine import Connection

from infra.db.schema.identity import security_identity, security_ticker_history

__all__ = ["SecurityLabel", "labels_as_of", "tickers_as_of"]


@dataclass(frozen=True, slots=True)
class SecurityLabel:
    """How to name one security to a human, at one instant.

    `ticker` is optional because it genuinely can be absent — a security
    whose ticker window closed, or one registered without one. A caller
    displaying a label has to decide what to do about that, which is why
    this returns `None` rather than a placeholder string that would be
    indistinguishable from a real ticker.
    """

    security_id: UUID
    ticker: str | None
    name: str | None

    def display(self) -> str:
        """A short human label: `AAPL`, `AAPL — Apple Inc.`, or the id.

        Falls back to the id rather than to an empty string: a message
        naming nothing is worse than one naming something opaque, because
        the opaque one can at least be looked up.
        """
        if self.ticker and self.name:
            return f"{self.ticker} — {self.name}"
        if self.ticker:
            return self.ticker
        if self.name:
            return self.name
        return str(self.security_id)


def labels_as_of(
    connection: Connection,
    security_ids: list[UUID],
    *,
    as_of: datetime,
) -> dict[UUID, SecurityLabel]:
    """Ticker and name for each security, as known at `as_of`. One query.

    One query rather than one per security: every caller has a list —
    a watchlist page, a universe fetch, a batch of alerts — and a
    per-row lookup is the difference between one round trip and hundreds.

    A security with no ticker valid at `as_of` still appears in the
    result, with `ticker=None`. Omitting it would make a caller iterating
    the result silently drop securities, which is the failure this shape
    exists to prevent.
    """
    if not security_ids:
        return {}

    history = security_ticker_history
    rows = connection.execute(
        select(
            security_identity.c.id,
            security_identity.c.name,
            history.c.ticker,
        )
        .select_from(
            security_identity.outerjoin(
                history,
                (history.c.security_id == security_identity.c.id)
                & (history.c.valid_from <= as_of)
                & or_(history.c.valid_to.is_(None), history.c.valid_to > as_of),
            )
        )
        .where(security_identity.c.id.in_(security_ids))
    ).all()

    return {
        row.id: SecurityLabel(security_id=row.id, ticker=row.ticker, name=row.name) for row in rows
    }


def tickers_as_of(
    connection: Connection,
    security_ids: list[UUID],
    *,
    as_of: datetime,
) -> dict[UUID, str]:
    """Just the tickers, and only for securities that have one at `as_of`.

    The narrower half of `labels_as_of`, for callers that cannot use a
    security without a ticker at all — a provider fetch, for instance.
    Absence from the result is the answer, and it is the caller's to
    report.
    """
    return {
        security_id: label.ticker
        for security_id, label in labels_as_of(connection, security_ids, as_of=as_of).items()
        if label.ticker
    }
