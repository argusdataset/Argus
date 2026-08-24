"""Reading a company: identity, statements, valuation. No computation.

Every read here goes through Module 07's PIT query layer rather than
touching `canonical_*` directly. That is not ceremony. `get_latest_fundamental_as_of`
encodes a correction that is easy to get wrong and invisible when you do:
with Q1 filed in May, Q2 filed in August, and Q1 *restated* in September,
ordering on availability alone returns the Q1 restatement for an October
query — the wrong quarter, not a stale one. A hand-rolled query in this
module would have that bug and nothing would fail.

## "Current" still means "as of an instant"

The Terminal shows a user today's picture, so `as_of` defaults to now.
But it stays a plain argument all the way down, exactly as
`CROSS_CUTTING_REQUIREMENTS.md` requires — which means the same functions
serve a historical view for free, and more importantly means nothing here
can quietly acquire an implicit "just give me the latest row" path.

## Nothing computed, and the reason is provenance not laziness

`ValuationResponse` returns stored metrics. A P/E ratio derived in this
module would be a number no other part of ARGUS could reproduce, with no
`availability_time` of its own and no lineage. If a valuation metric is
worth having, it is worth Module 05 storing it with its provenance
intact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.engine import Connection

from core.data_validation.fundamentals import get_latest_fundamental_as_of
from core.data_validation.result import MissReason
from data.canonical_model.records import CanonicalStatementType
from data.normalization.identity import SecurityIdentityResolver
from infra.db.schema.identity import security_identity, security_ticker_history
from services.terminal.errors import security_not_found
from services.terminal.schemas import (
    CompanyProfile,
    FinancialStatement,
    FundamentalsResponse,
    Unavailable,
    ValuationResponse,
)

__all__ = [
    "STATEMENT_TYPES",
    "VALUATION_TYPES",
    "resolve_company",
    "read_fundamentals",
    "read_valuation",
]

#: Every statement type the Terminal asks for. The full set is returned
#: every time — found ones in `statements`, the rest in `unavailable` —
#: so a consumer never has to know which types exist to know what is
#: missing.
STATEMENT_TYPES: tuple[CanonicalStatementType, ...] = (
    CanonicalStatementType.INCOME_STATEMENT,
    CanonicalStatementType.BALANCE_SHEET,
    CanonicalStatementType.CASH_FLOW,
)

#: Valuation lives in these two. Module 05's taxonomy splits "metrics"
#: from "ratios" because the provider does; ARGUS does not merge them,
#: and neither does this.
VALUATION_TYPES: tuple[CanonicalStatementType, ...] = (
    CanonicalStatementType.KEY_METRICS,
    CanonicalStatementType.RATIOS,
)

_MISS_EXPLANATIONS: dict[MissReason, str] = {
    MissReason.NOT_YET_AVAILABLE: (
        "ARGUS has no such record that was knowable at this instant. Either it has "
        "not been ingested, or it was filed later than the requested cutoff."
    ),
}


def resolve_company(
    connection: Connection, ticker: str, *, as_of: datetime | None = None
) -> CompanyProfile:
    """Turn a ticker into ARGUS's identity for it, as of an instant.

    "As of an instant" is load-bearing rather than pedantic: tickers are
    recycled after a delisting, so *who* was trading as a given symbol has
    a different answer on different dates. Module 05's resolver enforces
    that with a database exclusion constraint; this just asks it.
    """
    moment = as_of or datetime.now(UTC)
    resolver = SecurityIdentityResolver(connection)
    security_id = resolver.try_resolve(ticker, moment)
    if security_id is None:
        raise security_not_found(ticker)
    return _profile(connection, security_id, ticker=ticker, as_of=moment)


def read_fundamentals(
    connection: Connection,
    ticker: str,
    *,
    as_of: datetime | None = None,
    statement_types: tuple[CanonicalStatementType, ...] = STATEMENT_TYPES,
) -> FundamentalsResponse:
    """The latest statement of each type knowable at `as_of`.

    A type with nothing to show lands in `unavailable` with its reason,
    never as a null or an absent key. See `schemas.py` on why that
    distinction is the contract's most important property.
    """
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)

    statements: dict[str, FinancialStatement] = {}
    unavailable: dict[str, Unavailable] = {}

    for statement_type in statement_types:
        result = get_latest_fundamental_as_of(
            connection, profile.security_id, statement_type, moment
        )
        if not result.found:
            unavailable[statement_type.value] = _unavailable(result.reason)
            continue
        value = result.unwrap()
        statements[statement_type.value] = FinancialStatement(
            statement_type=statement_type.value,
            fiscal_period=value.fiscal_period,
            fiscal_period_end=value.fiscal_period_end,
            availability_time=value.availability_time,
            data=dict(value.data),
        )

    return FundamentalsResponse(
        security=profile, as_of=moment, statements=statements, unavailable=unavailable
    )


def read_valuation(
    connection: Connection, ticker: str, *, as_of: datetime | None = None
) -> ValuationResponse:
    """Stored valuation metrics, merged across KEY_METRICS and RATIOS.

    Merged, not computed. Where both statements carry the same field name
    the first one wins and `sources` records which — so a consumer looking
    at a surprising number can find out where it came from without
    guessing.
    """
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)

    metrics: dict[str, object] = {}
    sources: dict[str, str] = {}
    unavailable: dict[str, Unavailable] = {}

    for statement_type in VALUATION_TYPES:
        result = get_latest_fundamental_as_of(
            connection, profile.security_id, statement_type, moment
        )
        if not result.found:
            unavailable[statement_type.value] = _unavailable(result.reason)
            continue
        for key, value in result.unwrap().data.items():
            if key in metrics:
                continue
            metrics[key] = value
            sources[key] = statement_type.value

    return ValuationResponse(
        security=profile,
        as_of=moment,
        metrics=metrics,
        sources=sources,
        unavailable=unavailable,
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _profile(
    connection: Connection, security_id: UUID, *, ticker: str, as_of: datetime
) -> CompanyProfile:
    name = connection.execute(
        select(security_identity.c.name).where(security_identity.c.id == security_id)
    ).scalar_one_or_none()

    row = connection.execute(
        select(security_ticker_history.c.ticker, security_ticker_history.c.exchange)
        .where(
            security_ticker_history.c.security_id == security_id,
            security_ticker_history.c.valid_from <= as_of,
        )
        .order_by(desc(security_ticker_history.c.valid_from))
        .limit(1)
    ).one_or_none()

    return CompanyProfile(
        security_id=security_id,
        # The ticker as of `as_of`, which for a historical query is not
        # necessarily the one the caller asked with.
        ticker=row.ticker if row is not None else ticker.upper(),
        name=name,
        exchange=row.exchange if row is not None else None,
        as_of=as_of,
    )


def _unavailable(reason: MissReason | None) -> Unavailable:
    resolved = reason or MissReason.NOT_YET_AVAILABLE
    return Unavailable(
        reason=resolved.value,
        explanation=_MISS_EXPLANATIONS.get(
            resolved, "ARGUS has no such record knowable at this instant."
        ),
    )
