"""Governance: what executives were paid, and what management said.

Two reads over `canonical_disclosures`, grouped because they answer the
same underlying question — *how is this company run, in its own words* —
and because both are prose-and-proxy material that ARGUS is careful to
pass through untouched.

## Compensation is the one type here that could leak

A fiscal year ends in December; the proxy disclosing what the CEO was
paid for it is filed the following spring. `translate_executive_compensation`
sources `observation_time` from the filing date and never from the year
end, so a March query does not see figures that were not filed until
April. This module inherits that for free by filtering on
`availability_time` like every other read — the correctness lives in the
translation, not here, and this note exists so a reader of the endpoint
knows where to look.

## Transcripts are returned, never summarised

`data` is the provider's payload including the call's full text. ARGUS
does not extract sentiment from it, does not summarise it, and nothing
downstream reads it — the same rule `news.py` states for articles, and
for the same reason: the moment a machine-produced reading of management
tone enters the system, it is a scoring input whether or not anyone meant
it to be.

## One executive is one row

Several officers share a fiscal year, so `translate_executive_compensation`
writes the person into the period label (`"2025:John Smith, CEO"`).
Without that the second officer's row would collide with the first on
`uq_disclosure_observation` and vanish. The consequence for this module
is that `limit` bounds *rows*, not years: a company disclosing five
executives fills the default panel with one year of pay.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.engine import Connection

from data.canonical_model.records import CanonicalDisclosureType
from services.terminal.company import resolve_company
from services.terminal.config import TerminalConfig
from services.terminal.schemas import (
    CompensationRecord,
    ExecutiveCompensationResponse,
    TranscriptRecord,
    TranscriptsResponse,
)
from services.terminal.stored import latest_disclosures, unavailable

__all__ = ["read_executive_compensation", "read_transcripts"]


def read_executive_compensation(
    connection: Connection,
    ticker: str,
    *,
    as_of: datetime | None = None,
    limit: int | None = None,
    config: TerminalConfig | None = None,
) -> ExecutiveCompensationResponse:
    """Proxy-statement compensation, newest period first.

    Each row is one executive for one fiscal year — see the module
    docstring on why `limit` therefore counts rows rather than years.
    """
    config = config or TerminalConfig()
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)

    rows, reason = latest_disclosures(
        connection,
        profile.security_id,
        CanonicalDisclosureType.EXECUTIVE_COMPENSATION,
        as_of=moment,
        limit=config.limits.bounded_disclosure_limit(limit),
    )

    return ExecutiveCompensationResponse(
        security=profile,
        as_of=moment,
        disclosures=[
            CompensationRecord(
                fiscal_period=row.fiscal_period,
                fiscal_period_end=row.fiscal_period_end,
                availability_time=row.availability_time,
                data=dict(row.data),
            )
            for row in rows
        ],
        unavailable=unavailable(reason) if reason is not None else None,
    )


def read_transcripts(
    connection: Connection,
    ticker: str,
    *,
    as_of: datetime | None = None,
    limit: int | None = None,
    config: TerminalConfig | None = None,
) -> TranscriptsResponse:
    """Earnings-call transcripts, newest call first. Text passed through.

    `held_at` is `fiscal_period_end`, which for a transcript is the call
    itself rather than a quarter boundary — a call is an event with a
    timestamp, and `translate_transcript` stores it as one.
    """
    config = config or TerminalConfig()
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)

    rows, reason = latest_disclosures(
        connection,
        profile.security_id,
        CanonicalDisclosureType.EARNINGS_TRANSCRIPT,
        as_of=moment,
        limit=config.limits.bounded_disclosure_limit(limit),
    )

    return TranscriptsResponse(
        security=profile,
        as_of=moment,
        transcripts=[
            TranscriptRecord(
                fiscal_period=row.fiscal_period,
                held_at=row.fiscal_period_end,
                availability_time=row.availability_time,
                data=dict(row.data),
            )
            for row in rows
        ],
        unavailable=unavailable(reason) if reason is not None else None,
    )
