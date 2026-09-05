"""What else is nearby: peers, fund holdings, and who owns this.

Three reads — two over `canonical_snapshots`, one over the 13F summaries
Module 26 ingests. They look unrelated and are filed
together because they are the same kind of claim — *this security stands
in a relationship to these other securities* — and because both are the
provider's classification rather than ARGUS's.

## ARGUS does not pick comparables

The peer list is FMP's. It is not a sector model, not a correlation
cluster, and not something Module 13 or Module 09 consults. Deriving a
peer group here would be inference, which is precisely the line Module 19
does not cross; showing the provider's list and saying whose it is stays
on the right side of it.

## Fund holdings, and the gap ARGUS cannot see

`FundHoldingsResponse` is empty for an operating company, correctly. But
ARGUS stores no security-type flag, so this module cannot tell an
operating company apart from a fund that was never ingested — both come
back `NEVER_INGESTED`. `core/ingestion/terminal_data.py` documents the
same gap from the write side, where it means holdings are fetched only
for explicitly named `fund_tickers` rather than discovered.

Saying that plainly beats inventing a security type from the peer list or
the absence of financial statements. A guess there would be wrong for
exactly the securities that matter — a newly listed ETF, a fund that
files unusually — and would be wrong silently.

## Institutional ownership is read, never derived

The percentage comes from FMP's own 13F summary — the
`institutional_ownership` table Module 26 writes, through the same
`FIELD_ALIASES` that stored it. ARGUS holds share counts and could
divide them by a float it holds separately; it does not, because that
number would be ARGUS's arithmetic wearing the provider's clothes, with
no `availability_time` of its own and nothing else in the system able to
reproduce it. The same objection `company.py` raises to a derived P/E.

Reading that table here is not a boundary crossing. The isolation tests
forbid `core/scoring`, `core/market_state`, `core/candidate_detection`
and `core/live_scanner` from touching it — the Terminal is the module it
was ingested *for*, and it reads the raw summaries rather than the
`*_signals` tables, which are Module 26's readings and not the Terminal's
to show.

**Insider ownership percentage has no provider figure at all.** FMP
reports insider transactions, not a held percentage, so the field is
absent rather than null — the same treatment short interest gets in
`schemas.py`, and for the same reason.

## One row, not one row per position

A disclosure is a statement about the portfolio as it stood, so
`translate_fund_holdings` stores the whole position list as a single
snapshot. `position_count` therefore comes from the stored payload rather
than from `len(holdings)` after any truncation, which keeps "how many
positions the fund disclosed" a fact about the fund rather than a fact
about this response.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.engine import Connection

from core.data_validation.result import MissReason
from core.ownership_signals.institutional import FIELD_ALIASES as OWNERSHIP_ALIASES
from data.canonical_model.records import CanonicalSnapshotType
from infra.db.schema.ownership_signals import institutional_ownership
from services.terminal.company import resolve_company
from services.terminal.schemas import FundHoldingsResponse, OwnershipResponse, PeersResponse
from services.terminal.stored import latest_snapshot, unavailable

__all__ = ["read_fund_holdings", "read_institutional_ownership", "read_peers"]


def read_peers(
    connection: Connection,
    ticker: str,
    *,
    as_of: datetime | None = None,
) -> PeersResponse:
    """The provider's peer group as of an instant.

    `translate_peers` already reduced FMP's two documented shapes — a
    list under `peers`, or one row per peer — to a list of symbols, so
    this reads that list rather than re-deriving it. A peer list that
    resolved to nothing comes back as an empty list on a found snapshot,
    which is a different fact from no snapshot at all: the provider
    answered and named nobody.
    """
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)

    row, reason = latest_snapshot(
        connection, profile.security_id, CanonicalSnapshotType.PEERS, as_of=moment
    )
    if row is None:
        return PeersResponse(security=profile, as_of=moment, unavailable=unavailable(reason))

    return PeersResponse(
        security=profile,
        as_of=moment,
        peers=_symbols(dict(row.data).get("peers")),
        observed_at=row.observation_time,
    )


def read_fund_holdings(
    connection: Connection,
    ticker: str,
    *,
    as_of: datetime | None = None,
) -> FundHoldingsResponse:
    """What this fund held when it last disclosed, as of an instant.

    Empty for an operating company — and see the module docstring on why
    that is reported as `NEVER_INGESTED` rather than as "not a fund".
    """
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)

    row, reason = latest_snapshot(
        connection, profile.security_id, CanonicalSnapshotType.FUND_HOLDINGS, as_of=moment
    )
    if row is None:
        return FundHoldingsResponse(security=profile, as_of=moment, unavailable=unavailable(reason))

    payload = dict(row.data)
    holdings = payload.get("holdings")
    positions: list[dict[str, Any]] = (
        [dict(item) for item in holdings if isinstance(item, dict)]
        if isinstance(holdings, list)
        else []
    )

    return FundHoldingsResponse(
        security=profile,
        as_of=moment,
        holdings=positions,
        # The count the fund disclosed, which is what the stored payload
        # recorded at write time — not `len(positions)`, so a payload
        # this module could not parse reports a gap rather than a
        # confidently wrong zero.
        position_count=_count(payload.get("position_count"), fallback=len(positions)),
        source=payload.get("source"),
        observed_at=row.observation_time,
    )


def read_institutional_ownership(
    connection: Connection,
    ticker: str,
    *,
    as_of: datetime | None = None,
) -> OwnershipResponse:
    """The newest 13F summary knowable at `as_of`. The provider's figures.

    13F is filed 45 days after a quarter closes, and Module 26's
    translation already encodes that lag in `availability_time`, so this
    is the ordinary cutoff filter and nothing more. A query in April sees
    Q4, not Q1 — correctly, because Q1's filers have not filed.
    """
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)

    row = connection.execute(
        select(institutional_ownership)
        .where(
            institutional_ownership.c.security_id == profile.security_id,
            institutional_ownership.c.availability_time <= moment,
        )
        .order_by(
            desc(institutional_ownership.c.year),
            desc(institutional_ownership.c.quarter),
            desc(institutional_ownership.c.observation_time),
        )
        .limit(1)
    ).one_or_none()

    if row is None:
        return OwnershipResponse(
            security=profile,
            as_of=moment,
            unavailable=unavailable(_why_no_ownership(connection, profile.security_id)),
        )

    payload = dict(row.data)
    return OwnershipResponse(
        security=profile,
        as_of=moment,
        fiscal_period=f"{row.year}-Q{row.quarter}",
        institutional_ownership_percent=_number(payload, "ownership_percent"),
        investors_holding=_integer(payload, "investors_holding"),
        total_shares=_number(payload, "total_shares"),
        data=payload,
        observed_at=row.observation_time,
    )


def _why_no_ownership(connection: Connection, security_id: UUID) -> MissReason:
    """Held but hidden, or never ingested. The distinction `stored.py` draws."""
    held = connection.execute(
        select(func.count())
        .select_from(institutional_ownership)
        .where(institutional_ownership.c.security_id == security_id)
        .limit(1)
    ).scalar_one()
    return MissReason.NOT_YET_AVAILABLE if held else MissReason.NEVER_INGESTED


def _resolved(payload: dict[str, Any], concept: str) -> Any:
    """The first alias that carries a value.

    Module 26's aliases rather than a second set: the figure was stored
    under whichever spelling FMP used, and a copy of that table here
    would drift from the one that did the storing.
    """
    for alias in OWNERSHIP_ALIASES[concept]:
        value = payload.get(alias)
        if value is not None and value != "":
            return value
    return None


def _number(payload: dict[str, Any], concept: str) -> float | None:
    """`None` stays `None`. An unreported percentage is not zero percent."""
    value = _resolved(payload, concept)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _integer(payload: dict[str, Any], concept: str) -> int | None:
    value = _resolved(payload, concept)
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _symbols(value: Any) -> list[str]:
    """The stored peer list, defensively.

    `translate_peers` normalises to a list of strings, so this is
    belt-and-braces against an older row written before it did — the
    tables are append-only, which means rows written under an earlier
    translation stay readable forever.
    """
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _count(value: Any, *, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
