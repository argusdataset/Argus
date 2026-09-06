"""Writing the same record twice must add nothing. Three keys that failed to.

Every writer in ARGUS is `ON CONFLICT DO NOTHING`, because the tables are
append-only and `DO UPDATE` would be refused by migration 0003's guard.
That arrangement only works if the conflict actually happens, and three
natural keys were built so that it could not:

| Table | What went wrong | Consequence |
|---|---|---|
| `institutional_ownership` | no `observation_time` in the key | the first observation of a quarter froze; every later, fuller one was discarded |
| `analyst_grades` | nullable `new_grade` in the key | a NULL matches nothing, so re-ingestion appends daily, forever |
| `insider_trades` | three nullable columns in the key | same, and a security refreshed daily accumulates unreadable trades |

The two NULL cases share one cause and are worth stating plainly:
**under Postgres's default rule, two NULLs in a unique key are not
equal.** A row whose key contains a NULL conflicts with nothing —
including with an identical copy of itself. `ON CONFLICT DO NOTHING`
then does nothing to stop anything, which is the opposite of its name.

Migration 0020 fixes all three: `observation_time` joins the first key,
and the other two become `UNIQUE NULLS NOT DISTINCT`.

## Why these tests write rows rather than inspect DDL

A test asserting "the constraint has NULLS NOT DISTINCT" would pass
against a database where the writer never reaches the constraint. These
insert the same row twice through the real writers and assert the table
holds one row — which is the property, rather than a proxy for it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from core.ownership_signals.config import OwnershipSignalConfig
from core.ownership_signals.insider import translate_insider_transaction, write_insider_trades
from core.ownership_signals.institutional import (
    latest_two_quarters,
    translate_institutional_ownership,
    write_institutional_ownership,
)
from data.normalization.terminal_records import translate_grade, write_grades
from data.provider_adapters.fmp.models import (
    AnalystGrade,
    FetchProvenance,
    InsiderTransaction,
    InstitutionalOwnershipSummary,
)
from infra.db.schema.ownership_signals import insider_trades, institutional_ownership
from infra.db.schema.terminal_data import analyst_grades

FETCHED_AT = datetime(2026, 3, 3, 21, 0, tzinfo=UTC)
LATER = FETCHED_AT + timedelta(days=7)


def _provenance(endpoint: str, *, fetched_at: datetime = FETCHED_AT) -> FetchProvenance:
    return FetchProvenance(endpoint=endpoint, url_path=f"/stable/{endpoint}", fetched_at=fetched_at)


def _count(connection: Connection, table, security_id: UUID) -> int:
    return connection.execute(
        select(func.count()).select_from(table).where(table.c.security_id == security_id)
    ).scalar_one()


# --------------------------------------------------------------------------
# institutional_ownership: a quarter is revised, and both versions survive
# --------------------------------------------------------------------------


def _summary(symbol: str, *, investors: int, fetched_at: datetime) -> InstitutionalOwnershipSummary:
    return InstitutionalOwnershipSummary(
        provenance=_provenance("institutional_ownership_summary", fetched_at=fetched_at),
        symbol=symbol,
        year=2025,
        quarter=4,
        raw={
            "symbol": symbol,
            "year": 2025,
            "quarter": 4,
            "investorsHolding": investors,
            "numberOf13Fshares": investors * 10_000,
            "ownershipPercent": 55.5,
        },
    )


def test_re_fetching_an_unchanged_quarter_writes_nothing(
    connection: Connection, registered_security: UUID
):
    """Idempotence, which the widened key must not cost.

    The same summary observed at the same instant is the same fact. Only
    an observation at a *later* instant is a revision — see the next
    test.
    """
    config = OwnershipSignalConfig()
    row = translate_institutional_ownership(
        _summary("AAA", investors=120, fetched_at=FETCHED_AT),
        registered_security,
        thresholds=config.thresholds,
    )

    first = write_institutional_ownership(connection, [row])
    second = write_institutional_ownership(connection, [row])

    assert first.inserted == 1
    assert second.inserted == 0
    assert _count(connection, institutional_ownership, registered_security) == 1


def test_a_fuller_later_filing_of_the_same_quarter_is_kept(
    connection: Connection, registered_security: UUID
):
    """The bug this key change exists for.

    13F filings arrive across the 45 days after a quarter closes. A first
    fetch that saw 120 of an eventual 340 filers used to freeze at 120
    permanently — and the next quarter's comparison then reported a
    215-institution exodus that never happened.

    Both observations are now rows, which is what an append-only PIT
    table is for: the 120 is still what ARGUS knew in February, and the
    340 is what it knew in March.
    """
    config = OwnershipSignalConfig()
    early = translate_institutional_ownership(
        _summary("AAA", investors=120, fetched_at=FETCHED_AT),
        registered_security,
        thresholds=config.thresholds,
    )
    revised = translate_institutional_ownership(
        _summary("AAA", investors=340, fetched_at=LATER),
        registered_security,
        thresholds=config.thresholds,
    )

    write_institutional_ownership(connection, [early])
    inserted = write_institutional_ownership(connection, [revised])

    assert inserted.inserted == 1
    assert _count(connection, institutional_ownership, registered_security) == 2


def test_the_reader_takes_the_newest_observation_of_a_quarter(
    connection: Connection, registered_security: UUID
):
    """Two rows for one quarter, and the reader must not compare it to itself.

    This is the other half of the fix and the easier half to forget:
    `latest_two_quarters` used to take the two newest *rows*, which after
    the key change would have been two observations of Q4 — a quarter
    compared against an earlier version of itself, reporting a change
    that is really just the filings that arrived in between.
    """
    config = OwnershipSignalConfig()
    rows = [
        translate_institutional_ownership(
            _summary("AAA", investors=count, fetched_at=when),
            registered_security,
            thresholds=config.thresholds,
        )
        for count, when in ((120, FETCHED_AT), (340, LATER))
    ]
    for row in rows:
        write_institutional_ownership(connection, [row])

    periods = latest_two_quarters(
        connection, [registered_security], as_of=LATER + timedelta(days=30)
    )

    quarters = periods[registered_security]
    assert len(quarters) == 1, "one quarter is on file, however many times it was observed"
    assert quarters[0].investors_holding == 340


# --------------------------------------------------------------------------
# analyst_grades: a NULL grade must still deduplicate
# --------------------------------------------------------------------------


def _grade(symbol: str, *, resolvable: bool) -> AnalystGrade:
    raw = {"gradingCompany": "Alpha Bank", "date": "2026-03-02", "action": "upgrade"}
    if resolvable:
        raw["newGrade"] = "Buy"
    else:
        # A spelling `FIELD_ALIASES` does not know. `translate_grade`
        # stores None rather than failing, which is the right call for a
        # field whose name is unconfirmed — and is exactly the case the
        # old key could not deduplicate.
        raw["ratingTo"] = "Buy"
    return AnalystGrade(provenance=_provenance("analyst_grades"), symbol=symbol, raw=raw)


@pytest.mark.parametrize("resolvable", [True, False])
def test_the_same_grade_written_twice_stays_one_row(
    connection: Connection, registered_security: UUID, resolvable: bool
):
    """Parameterised over both cases on purpose.

    The resolvable case always worked. The unresolvable one — where
    `new_grade` is NULL — is the one that appended a duplicate on every
    run, and only the two together show that the fix did not break the
    case that was fine.
    """
    row = translate_grade(_grade("AAA", resolvable=resolvable), registered_security)
    assert (row.new_grade is not None) is resolvable

    first = write_grades(connection, [row])
    second = write_grades(connection, [row])

    assert first.inserted == 1
    assert second.inserted == 0
    assert _count(connection, analyst_grades, registered_security) == 1


def test_a_daily_refresh_does_not_accumulate_unreadable_grades(
    connection: Connection, registered_security: UUID
):
    """The shape the bug actually took in production terms.

    A security in BREAKOUT_READY is refreshed daily and its whole grade
    history comes back each time. With a NULL in the key that is one new
    row per grade per day, into an append-only table, forever.
    """
    row = translate_grade(_grade("AAA", resolvable=False), registered_security)

    for _ in range(5):
        write_grades(connection, [row])

    assert _count(connection, analyst_grades, registered_security) == 1


# --------------------------------------------------------------------------
# insider_trades: three nullable columns in one key
# --------------------------------------------------------------------------


def _trade(symbol: str, *, readable: bool) -> InsiderTransaction:
    raw: dict[str, object] = {
        "symbol": symbol,
        "transactionDate": "2026-03-02",
        "filingDate": "2026-03-03",
    }
    if readable:
        raw |= {
            "transactionType": "P-Purchase",
            "reportingName": "Jane Roe",
            "securitiesTransacted": 1000,
            "price": 10.0,
        }
    return InsiderTransaction(
        provenance=_provenance("insider_trading_search"), symbol=symbol, raw=raw
    )


@pytest.mark.parametrize("readable", [True, False])
def test_the_same_insider_trade_written_twice_stays_one_row(
    connection: Connection, registered_security: UUID, readable: bool
):
    """The unreadable case is the one that mattered.

    `normalize_transaction_code` returns None for a code it does not
    recognise — deliberately, so an unknown code is never counted as a
    purchase. That correct decision also produced a NULL in the key, and
    with it a row that could never be deduplicated.
    """
    config = OwnershipSignalConfig()
    row = translate_insider_transaction(
        _trade("AAA", readable=readable), registered_security, thresholds=config.thresholds
    )
    assert (row.transaction_code is not None) is readable

    first = write_insider_trades(connection, [row])
    second = write_insider_trades(connection, [row])

    assert first.inserted == 1
    assert second.inserted == 0
    assert _count(connection, insider_trades, registered_security) == 1


def test_two_genuinely_different_trades_are_two_rows(
    connection: Connection, registered_security: UUID
):
    """Deduplication must not become collapsing.

    Making NULLs equal is a narrow change and this is the guard on its
    edge: two purchases by different people on the same day are two
    facts, and a key that merged them would understate a cluster — the
    exact figure `insider_cluster_signals` counts.
    """
    config = OwnershipSignalConfig()
    rows = []
    for person, quantity in (("Jane Roe", 1000), ("John Doe", 2000)):
        trade = _trade("AAA", readable=True)
        payload = dict(trade.raw) | {"reportingName": person, "securitiesTransacted": quantity}
        rows.append(
            translate_insider_transaction(
                InsiderTransaction(
                    provenance=_provenance("insider_trading_search"),
                    symbol="AAA",
                    raw=payload,
                ),
                registered_security,
                thresholds=config.thresholds,
            )
        )

    result = write_insider_trades(connection, rows)

    assert result.inserted == 2
    assert _count(connection, insider_trades, registered_security) == 2


def test_two_unreadable_trades_on_different_days_are_two_rows(
    connection: Connection, registered_security: UUID
):
    """NULLs equal on the nullable columns, `event_time` still distinguishing.

    Without this the fix could over-collapse: every unreadable trade for
    a security becoming one row regardless of when it happened would lose
    real history rather than duplicate it.
    """
    config = OwnershipSignalConfig()
    rows = []
    for day in ("2026-03-02", "2026-03-03"):
        payload = {"symbol": "AAA", "transactionDate": day, "filingDate": day}
        rows.append(
            translate_insider_transaction(
                InsiderTransaction(
                    provenance=_provenance("insider_trading_search"),
                    symbol="AAA",
                    raw=payload,
                ),
                registered_security,
                thresholds=config.thresholds,
            )
        )

    result = write_insider_trades(connection, rows)

    assert result.inserted == 2
    assert all(row.transaction_code is None for row in rows)
    assert all(row.quantity is None or isinstance(row.quantity, Decimal) for row in rows)
