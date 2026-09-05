"""Provider records to canonical rows, for the Ultimate-plan Terminal data.

Module 05's `translate.py` does this for bars, fundamentals, corporate
actions and news. This file does the same job for the eight data types
Module 19's Terminal added, and lives beside it rather than inside it for
the reason `core/ingestion/news_writer.py` gives: extending a stable
module was not this work's to do, and a new file is reviewable in a way
an edit to a load-bearing one is not.

## The leakage decision, per type

Same question every time — *when could ARGUS first have known this?* — and
the answers differ more than they look:

| Type | `event_time` | `observation_time` |
|---|---|---|
| Analyst estimates | when the forecast was published | the same instant |
| Price target, peers, fund holdings | when the state was observed | the same instant |
| Analyst grade | the action date the firm reported | the same instant |
| Executive compensation | fiscal year end | **the filing date, never the year end** |
| Earnings transcript | the call | the same instant |

**Executive compensation is the one that could leak**, and it leaks the
same way fundamentals would: a fiscal year ends in December and the proxy
disclosing what the CEO was paid is filed the following spring. Sourcing
`observation_time` from the year end would make those figures readable
months before they existed. So it follows `translate_fundamental`'s rule
exactly — filing date if the payload carries one, otherwise the fetch
time, and **never** the period end.

## When the provider gives no date at all

Most of these endpoints report a state without saying when it became
true: a price-target consensus is "the consensus", with no timestamp for
when it moved. The honest answer is that ARGUS knew it when it fetched
it, so `observation_time` falls back to `provenance.fetched_at`.

The consequence, stated rather than buried: **a replay of a date before
the fetch sees nothing.** That is correct — ARGUS genuinely did not know
the consensus then — and it is the same conservative direction
`core/risk_context/`'s event calendar takes for the same reason. What it
costs is that these series only become useful going forward, from the
first ingestion run onward.

## Field names are unconfirmed

FMP's Ultimate plan was not purchased when this was written. Every
concept below is tried under several plausible spellings, the untouched
payload is stored regardless, and the spelling that resolved is recorded
in `lineage.resolved_fields` — the pattern
`core/candidate_detection/eligibility/bankruptcy.py` established, for the
same reason: a mismatch should surface as a visibly unresolved field
rather than as a silently wrong row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from data.canonical_model.pit import DEFAULT_LAG_POLICY, PitTimestamps, session_close
from data.canonical_model.records import CanonicalDisclosureType, CanonicalSnapshotType
from data.normalization.persistence import DEFAULT_BATCH_SIZE, WriteResult
from data.provider_adapters.fmp.models import (
    AnalystEstimate,
    AnalystGrade,
    EarningsTranscript,
    ExecutiveCompensation,
    FmpRecord,
    FundHolding,
    PriceTarget,
    SecurityPeerGroup,
    TechnicalIndicatorPoint,
)
from infra.db.schema.terminal_data import (
    analyst_grades,
    canonical_disclosures,
    canonical_snapshots,
    technical_indicators,
)

__all__ = [
    "FIELD_ALIASES",
    "PRICE_TARGET_TYPES",
    "StoredDisclosure",
    "StoredGrade",
    "StoredIndicatorPoint",
    "StoredSnapshot",
    "TerminalLagPolicy",
    "TerminalTranslationError",
    "resolve_field",
    "translate_analyst_estimate",
    "translate_executive_compensation",
    "translate_fund_holdings",
    "translate_grade",
    "translate_peers",
    "translate_price_target",
    "translate_technical_indicator",
    "translate_transcript",
    "write_disclosures",
    "write_indicators",
    "write_grades",
    "write_snapshots",
]

#: Accepted spellings per concept, tried in order. Unconfirmed — see the
#: module docstring.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    # Which fiscal period a disclosure covers.
    "fiscal_period": ("period", "calendarYear", "fiscalYear", "year", "date"),
    "fiscal_period_end": ("date", "periodEnd", "fiscalPeriodEnd"),
    # When the provider says the record became true. `date` is included
    # because for a grade or a transcript that field *is* the event date.
    "observed_at": ("publishedDate", "acceptedDate", "filingDate", "updatedAt", "date"),
    # The same question for records where `date` means something else
    # entirely. On an analyst estimate, `date` is the period being
    # forecast — a *future* date — and reading it as an observation time
    # would mark a 2027 forecast knowable only in 2027, hiding every
    # forward estimate the endpoint exists to provide. That is the
    # opposite of a leak and just as wrong, so estimates resolve their
    # publication time from these keys only.
    "published_at": ("publishedDate", "updatedAt", "lastUpdated", "acceptedDate"),
    # Analyst grades.
    "grading_company": ("gradingCompany", "analystCompany", "company", "publisher"),
    "grade_action": ("action", "newsAction", "gradeAction"),
    "previous_grade": ("previousGrade", "previousGrading", "gradeFrom"),
    "new_grade": ("newGrade", "newGrading", "grade", "gradeTo"),
    # Transcripts.
    "transcript_year": ("year", "fiscalYear"),
    "transcript_quarter": ("quarter", "fiscalQuarter"),
    # Peers.
    "peers": ("peers", "peersList", "peerSymbols", "symbols"),
    # The bar a technical indicator point describes.
    "indicator_date": ("date", "datetime", "timestamp"),
    # Executive compensation.
    "compensation_year": ("year", "fiscalYear", "acceptedDate", "filingDate"),
    "filed_at": ("filingDate", "acceptedDate", "date"),
}

#: Which snapshot type each price-target endpoint writes. Keyed on
#: `PriceTarget.source`, the value the fetcher sets — see
#: `translate_price_target` on why these are two types and not one.
PRICE_TARGET_TYPES: dict[str, CanonicalSnapshotType] = {
    "consensus": CanonicalSnapshotType.PRICE_TARGET_CONSENSUS,
    "summary": CanonicalSnapshotType.PRICE_TARGET_SUMMARY,
}

_YEAR_PATTERN = re.compile(r"(19|20)\d{2}")


@dataclass(frozen=True, slots=True)
class TerminalLagPolicy:
    """How long after something is knowable before ARGUS could fetch it.

    A local counterpart to `data/canonical_model/pit.py`'s
    `ProviderLagPolicy`, kept here rather than added there for the reason
    the module docstring gives. Same discipline: erring late is safe,
    erring early is leakage, so these are generous.
    """

    #: Analyst output — estimates, targets, grades. Published to clients
    #: first and to aggregators after; an hour is a conservative guess at
    #: the gap for something that is public the moment it is issued.
    analyst: timedelta = timedelta(hours=1)

    #: Proxy-statement compensation, measured from the filing date. Same
    #: 24 hours `ProviderLagPolicy.fundamentals` allows for a vendor to
    #: parse and publish an SEC filing.
    governance: timedelta = timedelta(hours=24)

    #: Transcripts. Published within hours of a call, rarely instantly.
    transcript: timedelta = timedelta(hours=12)

    #: Reference and holdings data. A fund discloses on its own schedule
    #: and aggregators refresh in bulk.
    reference: timedelta = timedelta(hours=24)


DEFAULT_TERMINAL_LAG = TerminalLagPolicy()


class TerminalTranslationError(ValueError):
    """A record cannot be stored without inventing part of its key.

    Raised rather than defaulted, the rule `translate.py` established:
    every plausible default here would be a silent PIT lie or a row filed
    under a period it does not describe.
    """


@dataclass(frozen=True, slots=True)
class StoredDisclosure:
    """One period-keyed row, ready for `canonical_disclosures`."""

    security_id: UUID
    disclosure_type: CanonicalDisclosureType
    fiscal_period: str
    fiscal_period_end: datetime | None
    pit: PitTimestamps
    data: dict[str, Any]
    lineage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    """One rolling-state row, ready for `canonical_snapshots`."""

    security_id: UUID
    snapshot_type: CanonicalSnapshotType
    pit: PitTimestamps
    data: dict[str, Any]
    lineage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredIndicatorPoint:
    """One indicator value for one bar, ready for `technical_indicators`."""

    security_id: UUID
    indicator: str
    period_length: int
    timeframe: str
    pit: PitTimestamps
    value: Decimal | None
    data: dict[str, Any]
    lineage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredGrade:
    """One rating action, ready for `analyst_grades`."""

    security_id: UUID
    pit: PitTimestamps
    grading_company: str
    action: str | None
    previous_grade: str | None
    new_grade: str | None
    data: dict[str, Any]
    lineage: dict[str, Any]


# --------------------------------------------------------------------------
# Field resolution
# --------------------------------------------------------------------------


def resolve_field(payload: dict[str, Any], concept: str) -> tuple[Any, str | None]:
    """First resolvable alias for `concept`, with the key that worked."""
    for alias in FIELD_ALIASES[concept]:
        value = payload.get(alias)
        if value is None or value == "":
            continue
        return value, alias
    return None, None


def _lineage(record: FmpRecord, resolved: dict[str, str | None], **extra: Any) -> dict[str, Any]:
    return {
        "provider": record.provenance.provider,
        "endpoint": record.provenance.endpoint,
        "from_cache": record.provenance.from_cache,
        "resolved_fields": resolved,
        **extra,
    }


def _as_datetime(value: Any, *, end_of_day: bool = True) -> datetime | None:
    """A provider timestamp, tolerant of the shapes FMP uses.

    A bare date anchors to end of day rather than midnight: a date alone
    does not say what time something landed, and assuming midnight would
    make it readable a full day early — `translate_fundamental`'s rule.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, time.max if end_of_day else time.min, tzinfo=UTC)

    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            day = date.fromisoformat(text[:10])
        except ValueError:
            return None
        return datetime.combine(day, time.max if end_of_day else time.min, tzinfo=UTC)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # A parsed value that is exactly midnight came from a bare date.
    if end_of_day and parsed.timetz() == time.min.replace(tzinfo=parsed.tzinfo):
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def _period_label(value: Any) -> str | None:
    """A period label as the provider wrote it, lightly normalised.

    Kept as text rather than parsed into a number: `"2026"`, `"FY2026"`
    and `"2026-Q2"` all identify a period, and reshaping them would make
    a stored label something a reader could not match against the source.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # A full date identifies its period by its year for annual data; the
    # caller passes a more specific label when it has one.
    return text[:10] if len(text) >= 10 and text[4:5] == "-" else text


def _year_from(value: Any) -> str | None:
    if value is None:
        return None
    match = _YEAR_PATTERN.search(str(value))
    return match.group(0) if match else None


# --------------------------------------------------------------------------
# Disclosures
# --------------------------------------------------------------------------


def translate_analyst_estimate(
    estimate: AnalystEstimate,
    security_id: UUID,
    *,
    lag: TerminalLagPolicy = DEFAULT_TERMINAL_LAG,
) -> StoredDisclosure:
    """One consensus forecast for one fiscal period.

    `event_time` is when the forecast was published rather than when the
    period it describes ends — the event is analysts publishing a view,
    and the period is what the view is *about*. Anchoring to the period
    would put `event_time` in the future for a forward estimate, which is
    not what that column means anywhere else in ARGUS.

    Most payloads carry no publication timestamp, so both times fall back
    to the fetch instant. See the module docstring on what that costs.
    """
    payload = dict(estimate.raw)
    period_value, period_key = resolve_field(payload, "fiscal_period")
    period = _period_label(period_value)
    if period is None:
        raise TerminalTranslationError(
            f"Analyst estimate for {estimate.symbol!r} names no fiscal period. "
            f"Tried {FIELD_ALIASES['fiscal_period']}; refusing to file it under a guess."
        )

    # `published_at`, not `observed_at`: on this endpoint `date` is the
    # period being forecast, and treating a 2027 period as the moment the
    # forecast became knowable would hide every forward estimate until
    # its own period arrived. See FIELD_ALIASES.
    observed_value, observed_key = resolve_field(payload, "published_at")
    observed = _as_datetime(observed_value) or estimate.provenance.fetched_at
    period_end_value, period_end_key = resolve_field(payload, "fiscal_period_end")

    return StoredDisclosure(
        security_id=security_id,
        disclosure_type=CanonicalDisclosureType.ANALYST_ESTIMATES,
        fiscal_period=period,
        fiscal_period_end=_as_datetime(period_end_value),
        pit=PitTimestamps.derive(
            event_time=observed,
            observation_time=observed,
            ingestion_time=estimate.provenance.fetched_at,
            lag=lag.analyst,
        ),
        data=payload,
        lineage=_lineage(
            estimate,
            {
                "fiscal_period": period_key,
                "published_at": observed_key,
                "fiscal_period_end": period_end_key,
            },
            observation_source="provider" if observed_value else "fetch_time",
        ),
    )


def translate_executive_compensation(
    compensation: ExecutiveCompensation,
    security_id: UUID,
    *,
    lag: TerminalLagPolicy = DEFAULT_TERMINAL_LAG,
) -> StoredDisclosure:
    """One executive's pay for one fiscal year.

    **The one type here that could leak.** A fiscal year ends in December
    and the proxy disclosing it is filed the following spring, so
    `observation_time` comes from the filing date and falls back to the
    fetch time — never to the year end, which is always available and
    always wrong, exactly as `translate_fundamental` refuses to do.

    One record per executive per year, so the row key includes the
    executive's name: two officers in the same year are two disclosures,
    not one that overwrites the other.
    """
    payload = dict(compensation.raw)
    year_value, year_key = resolve_field(payload, "compensation_year")
    year = _year_from(year_value)
    if year is None:
        raise TerminalTranslationError(
            f"Executive compensation for {compensation.symbol!r} names no fiscal year. "
            f"Tried {FIELD_ALIASES['compensation_year']}."
        )

    # Several executives share a year, so the period label carries the
    # person. Without it the second officer's row would collide with the
    # first on `uq_disclosure_observation` and be silently dropped.
    officer = payload.get("nameAndPosition") or payload.get("name") or payload.get("officerName")
    period = f"{year}:{officer}" if officer else year

    filed_value, filed_key = resolve_field(payload, "filed_at")
    filed = _as_datetime(filed_value)
    observed = filed or compensation.provenance.fetched_at
    year_end = datetime(int(year), 12, 31, 23, 59, 59, tzinfo=UTC)

    return StoredDisclosure(
        security_id=security_id,
        disclosure_type=CanonicalDisclosureType.EXECUTIVE_COMPENSATION,
        fiscal_period=period,
        fiscal_period_end=year_end,
        pit=PitTimestamps.derive(
            event_time=year_end,
            observation_time=observed,
            ingestion_time=compensation.provenance.fetched_at,
            lag=lag.governance,
        ),
        data=payload,
        lineage=_lineage(
            compensation,
            {"compensation_year": year_key, "filed_at": filed_key},
            observation_source="filing" if filed else "fetch_time",
        ),
    )


def translate_transcript(
    transcript: EarningsTranscript,
    security_id: UUID,
    *,
    lag: TerminalLagPolicy = DEFAULT_TERMINAL_LAG,
) -> StoredDisclosure:
    """One earnings call, as text.

    A call is public as it happens, so `event_time` and `observation_time`
    are the same instant. The transcript's text never changes afterwards,
    which is why its endpoint is the one marked immutable.
    """
    payload = dict(transcript.raw)
    year_value, year_key = resolve_field(payload, "transcript_year")
    quarter_value, quarter_key = resolve_field(payload, "transcript_quarter")

    year = transcript.year if transcript.year is not None else _int(year_value)
    quarter = transcript.quarter if transcript.quarter is not None else _int(quarter_value)
    if year is None:
        raise TerminalTranslationError(
            f"Earnings transcript for {transcript.symbol!r} names no year."
        )

    period = f"{year}-Q{quarter}" if quarter else str(year)
    held_value, held_key = resolve_field(payload, "observed_at")
    held = _as_datetime(held_value) or transcript.provenance.fetched_at

    return StoredDisclosure(
        security_id=security_id,
        disclosure_type=CanonicalDisclosureType.EARNINGS_TRANSCRIPT,
        fiscal_period=period,
        fiscal_period_end=held,
        pit=PitTimestamps.derive(
            event_time=held,
            observation_time=held,
            ingestion_time=transcript.provenance.fetched_at,
            lag=lag.transcript,
        ),
        data=payload,
        lineage=_lineage(
            transcript,
            {
                "transcript_year": year_key,
                "transcript_quarter": quarter_key,
                "observed_at": held_key,
            },
        ),
    )


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------


def _snapshot(
    record: FmpRecord,
    security_id: UUID,
    snapshot_type: CanonicalSnapshotType,
    *,
    lag: timedelta,
    payload: dict[str, Any],
    resolved: dict[str, str | None],
    observed: datetime | None,
) -> StoredSnapshot:
    moment = observed or record.provenance.fetched_at
    return StoredSnapshot(
        security_id=security_id,
        snapshot_type=snapshot_type,
        pit=PitTimestamps.derive(
            event_time=moment,
            observation_time=moment,
            ingestion_time=record.provenance.fetched_at,
            lag=lag,
        ),
        data=payload,
        lineage=_lineage(
            record,
            resolved,
            observation_source="provider" if observed else "fetch_time",
        ),
    )


def translate_price_target(
    target: PriceTarget,
    security_id: UUID,
    *,
    lag: TerminalLagPolicy = DEFAULT_TERMINAL_LAG,
) -> StoredSnapshot:
    """A consensus target, or the counts behind one.

    The two endpoints become two **snapshot types**, not one type with a
    `source` field. That distinction is load-bearing: the snapshot key is
    (security, type, observation_time), and both halves are fetched in
    the same run, so under one type any pair whose observation instants
    agree — two payloads carrying the same `date`, or a cache hit
    replaying one fetch time — collides on `uq_snapshot_observation` and
    the second is dropped by `ON CONFLICT DO NOTHING`. Which half
    survived would depend on request ordering. Two types cannot collide.

    `source` is still written into the payload, because a row read on its
    own should say what it is without the caller having to consult the
    column it was filed under.
    """
    payload = {**target.raw, "source": target.source}
    observed_value, observed_key = resolve_field(target.raw, "observed_at")
    return _snapshot(
        target,
        security_id,
        PRICE_TARGET_TYPES.get(target.source, CanonicalSnapshotType.PRICE_TARGET_CONSENSUS),
        lag=lag.analyst,
        payload=payload,
        resolved={"observed_at": observed_key},
        observed=_as_datetime(observed_value),
    )


def translate_peers(
    group: SecurityPeerGroup,
    security_id: UUID,
    *,
    lag: TerminalLagPolicy = DEFAULT_TERMINAL_LAG,
) -> StoredSnapshot:
    """The provider's peer list, normalised to a list of symbols.

    FMP is documented as returning either a list under `peers` or one row
    per peer; both shapes reduce to the same stored list, and the raw
    payload is kept beside it either way.
    """
    payload = dict(group.raw)
    peers_value, peers_key = resolve_field(payload, "peers")

    symbols: list[str] = []
    if isinstance(peers_value, list):
        symbols = [str(item) for item in peers_value if item]
    elif isinstance(peers_value, str):
        symbols = [part.strip() for part in peers_value.split(",") if part.strip()]
    elif peers_value is not None:
        symbols = [str(peers_value)]

    observed_value, observed_key = resolve_field(payload, "observed_at")
    return _snapshot(
        group,
        security_id,
        CanonicalSnapshotType.PEERS,
        lag=lag.reference,
        payload={**payload, "peers": symbols},
        resolved={"peers": peers_key, "observed_at": observed_key},
        observed=_as_datetime(observed_value),
    )


def translate_fund_holdings(
    holdings: list[FundHolding],
    security_id: UUID,
    *,
    lag: TerminalLagPolicy = DEFAULT_TERMINAL_LAG,
) -> StoredSnapshot:
    """A fund's whole position list as one row.

    One row rather than one per position, because a disclosure is a
    statement about the portfolio as it stood — a row per holding would
    make "what did this fund hold in March" a reconstruction rather than
    a lookup, and would multiply an S&P 500 ETF into five hundred rows
    per fetch.
    """
    if not holdings:
        raise TerminalTranslationError("A fund holdings snapshot needs at least one position.")

    first = holdings[0]
    observed_value, observed_key = resolve_field(dict(first.raw), "observed_at")
    return _snapshot(
        first,
        security_id,
        CanonicalSnapshotType.FUND_HOLDINGS,
        lag=lag.reference,
        payload={
            "source": first.source,
            "position_count": len(holdings),
            "holdings": [dict(holding.raw) for holding in holdings],
        },
        resolved={"observed_at": observed_key},
        observed=_as_datetime(observed_value),
    )


# --------------------------------------------------------------------------
# Grades
# --------------------------------------------------------------------------


def translate_grade(
    grade: AnalystGrade,
    security_id: UUID,
    *,
    lag: TerminalLagPolicy = DEFAULT_TERMINAL_LAG,
) -> StoredGrade:
    """One firm's rating action.

    A rating change is public when the firm issues it, so `event_time`
    and `observation_time` are both the action date. The firm's own
    wording for the action is stored unchanged: ARGUS does not decide
    what a house meant by "Market Perform".
    """
    payload = dict(grade.raw)
    company_value, company_key = resolve_field(payload, "grading_company")
    if not company_value:
        raise TerminalTranslationError(
            f"Analyst grade for {grade.symbol!r} names no grading company. "
            f"Tried {FIELD_ALIASES['grading_company']}; it is half the row's key."
        )

    acted_value, acted_key = resolve_field(payload, "observed_at")
    acted = _as_datetime(acted_value)
    if acted is None:
        raise TerminalTranslationError(
            f"Analyst grade for {grade.symbol!r} from {company_value!r} has no date."
        )

    action_value, action_key = resolve_field(payload, "grade_action")
    previous_value, previous_key = resolve_field(payload, "previous_grade")
    new_value, new_key = resolve_field(payload, "new_grade")

    return StoredGrade(
        security_id=security_id,
        pit=PitTimestamps.derive(
            event_time=acted,
            observation_time=acted,
            ingestion_time=grade.provenance.fetched_at,
            lag=lag.analyst,
        ),
        grading_company=str(company_value),
        action=str(action_value) if action_value else None,
        previous_grade=str(previous_value) if previous_value else None,
        new_grade=str(new_value) if new_value else None,
        data=payload,
        lineage=_lineage(
            grade,
            {
                "grading_company": company_key,
                "observed_at": acted_key,
                "grade_action": action_key,
                "previous_grade": previous_key,
                "new_grade": new_key,
            },
        ),
    )


# --------------------------------------------------------------------------
# Technical indicators
# --------------------------------------------------------------------------


def translate_technical_indicator(
    point: TechnicalIndicatorPoint,
    security_id: UUID,
) -> StoredIndicatorPoint:
    """One indicator value for one closed bar.

    `event_time` is the session close of the bar the value describes, and
    availability follows `ProviderLagPolicy.daily_bar` — the same sixteen
    hours a daily bar itself waits, because an indicator computed from a
    bar cannot be knowable before the bar is. Reusing that constant
    rather than inventing a second one keeps the two from drifting: if
    the bar lag is ever wrong, it is wrong in one place.

    **Which JSON key holds the number differs per indicator** — `rsi`
    under `"rsi"`, ADX under `"adx"`, and so on — so the indicator's own
    name is tried first, then its camelCase spelling, then a generic
    `value`. The whole provider row is stored regardless, so an
    unresolved value is a null column beside a complete payload rather
    than a lost fetch.
    """
    payload = dict(point.raw)
    date_value, date_key = resolve_field(payload, "indicator_date")
    moment = _as_datetime(date_value)
    if moment is None:
        raise TerminalTranslationError(
            f"{point.indicator} point for {point.symbol!r} has no date; "
            f"tried {FIELD_ALIASES['indicator_date']}."
        )

    # A bare date means a daily bar: anchor to that session's close so the
    # point sits on the same instant Module 05 gives the bar itself.
    if moment.hour == 23 and moment.minute == 59:
        moment = session_close(moment.date())

    value, value_key = _indicator_value(payload, point.indicator)
    return StoredIndicatorPoint(
        security_id=security_id,
        indicator=point.indicator,
        period_length=point.period_length,
        timeframe=point.timeframe,
        pit=PitTimestamps.derive(
            event_time=moment,
            observation_time=moment,
            ingestion_time=point.provenance.fetched_at,
            lag=DEFAULT_LAG_POLICY.daily_bar,
        ),
        value=value,
        data=payload,
        lineage=_lineage(point, {"indicator_date": date_key, "value": value_key}),
    )


def _indicator_value(payload: dict[str, Any], indicator: str) -> tuple[Decimal | None, str | None]:
    """The indicator's own number, under whichever key holds it."""
    candidates = (
        indicator,
        indicator.lower(),
        _camel(indicator),
        "value",
    )
    for key in candidates:
        raw = payload.get(key)
        if raw is None or raw == "":
            continue
        try:
            return Decimal(str(raw)), key
        except (InvalidOperation, ValueError):
            continue
    return None, None


def _camel(indicator: str) -> str:
    """`standarddeviation` -> `standardDeviation`, the spelling FMP uses."""
    known = {"standarddeviation": "standardDeviation", "williams": "williamsR"}
    return known.get(indicator, indicator)


# --------------------------------------------------------------------------
# Writers — insert-only, matching the append-only guard on all four tables
# --------------------------------------------------------------------------


def write_disclosures(
    connection: Connection,
    rows: list[StoredDisclosure],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> WriteResult:
    """Insert-only. A revision is a new row with a later observation."""
    return _insert(
        connection,
        canonical_disclosures,
        [
            {
                "security_id": row.security_id,
                "disclosure_type": row.disclosure_type.value,
                "fiscal_period": row.fiscal_period,
                "fiscal_period_end": row.fiscal_period_end,
                **row.pit.as_columns(),
                "data": row.data,
                "lineage": row.lineage,
            }
            for row in rows
        ],
        constraint="uq_disclosure_observation",
        batch_size=batch_size,
    )


def write_snapshots(
    connection: Connection,
    rows: list[StoredSnapshot],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> WriteResult:
    """Insert-only. Re-fetching an unchanged state inserts nothing."""
    return _insert(
        connection,
        canonical_snapshots,
        [
            {
                "security_id": row.security_id,
                "snapshot_type": row.snapshot_type.value,
                **row.pit.as_columns(),
                "data": row.data,
                "lineage": row.lineage,
            }
            for row in rows
        ],
        constraint="uq_snapshot_observation",
        batch_size=batch_size,
    )


def write_grades(
    connection: Connection,
    rows: list[StoredGrade],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> WriteResult:
    """Insert-only. A repeated action from the same firm is the same fact."""
    return _insert(
        connection,
        analyst_grades,
        [
            {
                "security_id": row.security_id,
                **row.pit.as_columns(),
                "grading_company": row.grading_company,
                "action": row.action,
                "previous_grade": row.previous_grade,
                "new_grade": row.new_grade,
                "data": row.data,
                "lineage": row.lineage,
            }
            for row in rows
        ],
        constraint="uq_analyst_grade_action",
        batch_size=batch_size,
    )


def write_indicators(
    connection: Connection,
    rows: list[StoredIndicatorPoint],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> WriteResult:
    """Insert-only, first write wins.

    Unlike the other three, the key here carries no `observation_time`:
    an indicator over a closed bar is settled, so a re-fetch offers the
    same number and a second row for it would only grow the table.
    """
    return _insert(
        connection,
        technical_indicators,
        [
            {
                "security_id": row.security_id,
                "indicator": row.indicator,
                "period_length": row.period_length,
                "timeframe": row.timeframe,
                **row.pit.as_columns(),
                "value": row.value,
                "data": row.data,
                "lineage": row.lineage,
            }
            for row in rows
        ],
        constraint="uq_technical_indicator_point",
        batch_size=batch_size,
    )


def _insert(
    connection: Connection,
    table: Any,
    values: list[dict[str, Any]],
    *,
    constraint: str,
    batch_size: int,
) -> WriteResult:
    """`ON CONFLICT DO NOTHING`, never `DO UPDATE`.

    Not a preference: all four tables carry migration 0003's append-only
    trigger, so an UPDATE would be refused by the database. The constraint
    and the guard agree, which is the arrangement `persistence.py` chose
    for the same reason.
    """
    result = WriteResult(offered=len(values))
    for start in range(0, len(values), batch_size):
        batch = values[start : start + batch_size]
        if not batch:
            continue
        statement = (
            insert(table)
            .values(batch)
            .on_conflict_do_nothing(constraint=constraint)
            .returning(table.c.id)
        )
        result.inserted += len(connection.execute(statement).fetchall())
    return result


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
