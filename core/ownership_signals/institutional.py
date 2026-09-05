"""The 13F institutional-ownership trend: who owns this, and is that growing?

## A trend, not an event — so there is no `raised`

Every other signal in Modules 28 and 29 answers a yes/no question about a
day. This one does not, and the brief was explicit about why: quarterly
institutional ownership moving up or down is a *direction*, not something
that happened. A boolean would have to encode somebody's opinion about
how much of a change matters, and that opinion belongs to the person
reading the number, not to ARGUS.

So the stored row carries figures and a percentage change, and stops
there. `institutional_ownership_signals` has no `raised` column at all —
not a nullable one, none — so there is nothing for a future edit to
quietly start filling in.

## What is stored, and where each number comes from

| Field | Meaning |
|---|---|
| `investors_holding` | How many institutions reported a position this quarter |
| `investors_holding_change` | Against the prior quarter. Provider-supplied when available, else computed |
| `total_shares` | Shares held across all reporting institutions |
| `total_shares_change_percent` | Quarter-over-quarter change in that total, as a percentage |
| `ownership_percent` | Institutional holdings as a share of the float, as the provider reports it |
| `prior_year` / `prior_quarter` | Which quarter the comparison was made against, so a reader can check it |

`total_shares_change_percent` is computed here rather than taken from the
provider even where a change field exists, because a percentage needs a
denominator and only the two absolute figures say what it was. The
investor *count* change is taken from the provider when offered, since
that one is a plain subtraction the provider is unlikely to get wrong and
its own value is the more authoritative statement of what it observed.

## The 45-day rule is the leak this module could most easily have

Institutional managers have 45 days after a quarter ends to file their
13F. Treating a quarter's holdings as knowable at the quarter end would
hand a backtest six weeks of hindsight on exactly the accumulation this
module exists to surface — the single largest leak available here. So
`availability_time` is the quarter end plus
`institutional_availability_lag_days`, every read filters on it, and the
constant is tagged `structural` because it follows from an SEC deadline
rather than from anyone's judgement.

## Field names are unconfirmed

Same caveat as everywhere else in this work: FMP's Ultimate plan was not
purchased when this was written, so `FIELD_ALIASES` lists several
plausible spellings per concept and the stored row records which one
resolved — `core/candidate_detection/eligibility/bankruptcy.py`'s
pattern, for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.data_validation.result import MissReason
from core.ownership_signals.config import OwnershipSignalConfig, OwnershipThresholds
from data.canonical_model.pit import PitTimestamps
from data.normalization.persistence import DEFAULT_BATCH_SIZE, WriteResult
from data.provider_adapters.fmp.models import InstitutionalOwnershipSummary
from infra.db.schema.ownership_signals import (
    institutional_ownership,
    institutional_ownership_signals,
)

__all__ = [
    "FIELD_ALIASES",
    "InstitutionalPeriod",
    "InstitutionalTrend",
    "OwnershipQuarter",
    "StoredOwnership",
    "assess_institutional_batch",
    "evaluate_institutional_trend",
    "latest_two_quarters",
    "quarter_end",
    "store_institutional_signals",
    "translate_institutional_ownership",
    "write_institutional_ownership",
]

#: Accepted spellings per concept, tried in order. Unconfirmed — see the
#: module docstring.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "investors_holding": (
        "investorsHolding",
        "investorCount",
        "numberOfInvestors",
        "holders",
    ),
    "investors_holding_change": (
        "investorsHoldingChange",
        "changeInInvestorsHolding",
        "investorHoldingChange",
    ),
    "total_shares": (
        "numberOf13Fshares",
        "numberOf13FShares",
        "totalShares",
        "sharesHeld",
    ),
    "ownership_percent": (
        "ownershipPercent",
        "institutionalOwnershipPercent",
        "percentOfSharesOutstanding",
    ),
    "year": ("year", "fiscalYear"),
    "quarter": ("quarter", "fiscalQuarter"),
}


class OwnershipTranslationError(ValueError):
    """A 13F summary cannot be stored without inventing which quarter it is.

    Raised rather than defaulted: a summary filed under the wrong quarter
    would be compared against the wrong prior quarter, and the resulting
    change figure would look perfectly reasonable while being nonsense.
    """


@dataclass(frozen=True, slots=True)
class OwnershipQuarter:
    """A calendar quarter, as 13F reports them."""

    year: int
    quarter: int

    def previous(self) -> OwnershipQuarter:
        if self.quarter == 1:
            return OwnershipQuarter(self.year - 1, 4)
        return OwnershipQuarter(self.year, self.quarter - 1)

    def as_tuple(self) -> tuple[int, int]:
        return (self.year, self.quarter)


@dataclass(frozen=True, slots=True)
class StoredOwnership:
    """One 13F summary, ready for `institutional_ownership`."""

    security_id: UUID
    period: OwnershipQuarter
    pit: PitTimestamps
    lineage: dict[str, Any]
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InstitutionalPeriod:
    """One quarter's figures, read back out of a stored row."""

    period: OwnershipQuarter
    investors_holding: int | None
    investors_holding_change: int | None
    total_shares: Decimal | None
    ownership_percent: Decimal | None


@dataclass(frozen=True, slots=True)
class InstitutionalTrend:
    """One security's ownership trend for one quarter. No verdict attached."""

    security_id: UUID
    period: OwnershipQuarter | None
    as_of: datetime
    investors_holding: int | None
    investors_holding_change: int | None
    total_shares: Decimal | None
    total_shares_change_percent: Decimal | None
    ownership_percent: Decimal | None
    prior_period: OwnershipQuarter | None
    config_version_label: str
    #: Set when there is no 13F data for this security at all.
    unavailable: MissReason | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "security_id": str(self.security_id),
            "year": self.period.year if self.period else None,
            "quarter": self.period.quarter if self.period else None,
            "as_of": self.as_of.isoformat(),
            "investors_holding": self.investors_holding,
            "investors_holding_change": self.investors_holding_change,
            "total_shares": _plain(self.total_shares),
            "total_shares_change_percent": _plain(self.total_shares_change_percent),
            "ownership_percent": _plain(self.ownership_percent),
            "prior_year": self.prior_period.year if self.prior_period else None,
            "prior_quarter": self.prior_period.quarter if self.prior_period else None,
            "config_version_label": self.config_version_label,
            "unavailable": self.unavailable.value if self.unavailable else None,
            "detail": dict(self.detail),
        }


# --------------------------------------------------------------------------
# Quarters
# --------------------------------------------------------------------------


def quarter_end(period: OwnershipQuarter) -> date:
    """The last day of a calendar quarter — the event this row describes."""
    last_month = period.quarter * 3
    if last_month == 12:
        return date(period.year, 12, 31)
    return date(period.year, last_month + 1, 1) - _ONE_DAY


# --------------------------------------------------------------------------
# Ingest: provider record -> stored row
# --------------------------------------------------------------------------


def resolve_field(payload: dict[str, Any], concept: str) -> tuple[Any, str | None]:
    """First resolvable alias for `concept`, with the key that worked."""
    for alias in FIELD_ALIASES[concept]:
        value = payload.get(alias)
        if value is None or value == "":
            continue
        return value, alias
    return None, None


def translate_institutional_ownership(
    summary: InstitutionalOwnershipSummary,
    security_id: UUID,
    *,
    thresholds: OwnershipThresholds | None = None,
) -> StoredOwnership:
    """One provider 13F summary, ready to store.

    The quarter comes from the request parameters when the fetcher was
    given them, and from the payload otherwise — either is a fact rather
    than a guess. When neither says, the row is rejected: see
    `OwnershipTranslationError` on why a mis-dated quarter is worse than
    a missing one.

    `event_time` is the quarter end — the period these holdings describe.
    `observation_time` and `availability_time` are that plus the 45-day
    filing deadline, because nothing about this quarter is public until
    managers have filed. Unlike a news article, the event and its
    observability are six weeks apart, and collapsing them is the leak
    this module most needed to avoid.
    """
    resolved_thresholds = thresholds or OwnershipThresholds()
    payload = dict(summary.raw)

    year = summary.year
    quarter = summary.quarter
    year_key = quarter_key = None
    if year is None:
        raw_year, year_key = resolve_field(payload, "year")
        year = _int(raw_year)
    if quarter is None:
        raw_quarter, quarter_key = resolve_field(payload, "quarter")
        quarter = _int(raw_quarter)

    if year is None or quarter is None or not 1 <= quarter <= 4:
        raise OwnershipTranslationError(
            f"13F summary for {summary.symbol!r} names no usable (year, quarter): "
            f"got ({year!r}, {quarter!r}). Refusing to guess which period it covers."
        )

    period = OwnershipQuarter(int(year), int(quarter))
    ends = datetime.combine(quarter_end(period), time.max, tzinfo=UTC)
    knowable = ends + resolved_thresholds.institutional_availability_lag

    return StoredOwnership(
        security_id=security_id,
        period=period,
        pit=PitTimestamps.derive(
            event_time=ends,
            observation_time=knowable,
            ingestion_time=summary.provenance.fetched_at,
            # The lag is already in `observation_time`; availability is
            # the same instant rather than the deadline applied twice.
            lag=_NO_EXTRA_LAG,
        ),
        lineage={
            "provider": summary.provenance.provider,
            "endpoint": summary.provenance.endpoint,
            "from_cache": summary.provenance.from_cache,
            "resolved_fields": {"year": year_key, "quarter": quarter_key},
        },
        data=payload,
    )


def write_institutional_ownership(
    connection: Connection,
    rows: list[StoredOwnership],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> WriteResult:
    """Insert-only, idempotent on `(security_id, year, quarter)`.

    A quarter re-fetched later inserts nothing: 13F figures for a closed
    quarter do not change, and the ones that do (a late or amended filing)
    arrive as a different quarter's row rather than as an edit to this one.
    """
    result = WriteResult(offered=len(rows))
    if not rows:
        return result

    values = [
        {
            "security_id": row.security_id,
            "year": row.period.year,
            "quarter": row.period.quarter,
            **row.pit.as_columns(),
            "lineage": row.lineage,
            "data": row.data,
        }
        for row in rows
    ]

    for start in range(0, len(values), batch_size):
        batch = values[start : start + batch_size]
        if not batch:
            continue
        statement = (
            insert(institutional_ownership)
            .values(batch)
            .on_conflict_do_nothing(constraint="uq_institutional_ownership_security_period")
            .returning(institutional_ownership.c.id)
        )
        result.inserted += len(connection.execute(statement).fetchall())
    return result


# --------------------------------------------------------------------------
# Assess: stored rows -> the quarter's trend
# --------------------------------------------------------------------------


def latest_two_quarters(
    connection: Connection,
    security_ids: list[UUID],
    *,
    as_of: datetime,
) -> dict[UUID, list[InstitutionalPeriod]]:
    """The two most recent knowable quarters per security, newest first.

    Two rather than one because the whole output is a comparison. PIT
    filtered on `availability_time <= as_of`: a quarter whose 45-day
    filing window has not closed at the cutoff being asked about is not
    part of that cutoff's answer, however complete the row is now.

    One query for every security. Ordering and slicing happen here rather
    than in SQL because "two per group" is a window function whose cost
    and readability are both worse than reading a few extra rows for a
    handful of quarters.
    """
    if not security_ids:
        return {}

    rows = connection.execute(
        select(
            institutional_ownership.c.security_id,
            institutional_ownership.c.year,
            institutional_ownership.c.quarter,
            institutional_ownership.c.data,
        )
        .where(
            institutional_ownership.c.security_id.in_(security_ids),
            institutional_ownership.c.availability_time <= as_of,
        )
        .order_by(
            institutional_ownership.c.security_id,
            institutional_ownership.c.year.desc(),
            institutional_ownership.c.quarter.desc(),
        )
    ).all()

    periods: dict[UUID, list[InstitutionalPeriod]] = {}
    for row in rows:
        collected = periods.setdefault(row.security_id, [])
        if len(collected) >= 2:
            continue
        collected.append(_period_from(row))
    return periods


def evaluate_institutional_trend(
    *,
    security_id: UUID,
    as_of: datetime,
    periods: list[InstitutionalPeriod] | None,
    config_version_label: str,
) -> InstitutionalTrend:
    """This quarter's figures and how they moved. No verdict, by design.

    With no stored quarter at all the result is `NEVER_INGESTED` and every
    figure is `None` — the same "absence is not a measurement" rule the
    rest of this project follows.

    With exactly one quarter the figures are reported and the *changes*
    are `None`: a first observation has nothing to have changed from, and
    reporting zero change would claim stability nobody observed.
    """
    if not periods:
        return InstitutionalTrend(
            security_id=security_id,
            period=None,
            as_of=as_of,
            investors_holding=None,
            investors_holding_change=None,
            total_shares=None,
            total_shares_change_percent=None,
            ownership_percent=None,
            prior_period=None,
            config_version_label=config_version_label,
            unavailable=MissReason.NEVER_INGESTED,
            detail={
                "reason": (
                    "No 13F summary knowable at this cutoff has ever been recorded "
                    "for this security."
                )
            },
        )

    current = periods[0]
    prior = periods[1] if len(periods) > 1 else None

    investors_change = current.investors_holding_change
    if (
        investors_change is None
        and prior is not None
        and current.investors_holding is not None
        and prior.investors_holding is not None
    ):
        investors_change = current.investors_holding - prior.investors_holding

    shares_change_percent = None
    if (
        prior is not None
        and current.total_shares is not None
        and prior.total_shares is not None
        and prior.total_shares > 0
    ):
        shares_change_percent = (
            (current.total_shares - prior.total_shares) / prior.total_shares
        ) * Decimal(100)

    return InstitutionalTrend(
        security_id=security_id,
        period=current.period,
        as_of=as_of,
        investors_holding=current.investors_holding,
        investors_holding_change=investors_change,
        total_shares=current.total_shares,
        total_shares_change_percent=shares_change_percent,
        ownership_percent=current.ownership_percent,
        prior_period=prior.period if prior else None,
        config_version_label=config_version_label,
        detail={
            "quarters_compared": (
                [current.period.as_tuple(), prior.period.as_tuple()]
                if prior
                else [current.period.as_tuple()]
            ),
            # Stated so a reader of a stored row can tell "no prior
            # quarter to compare against" from "compared, and it did not
            # move" — which look identical if only the change is shown.
            "prior_quarter_available": prior is not None,
            "investors_holding_change_source": (
                "provider" if current.investors_holding_change is not None else "computed"
            ),
        },
    )


def assess_institutional_batch(
    connection: Connection,
    security_ids: list[UUID],
    *,
    as_of: datetime,
    config: OwnershipSignalConfig | None = None,
) -> dict[UUID, InstitutionalTrend]:
    """Every security's ownership trend, from one query."""
    if not security_ids:
        return {}

    resolved = config or OwnershipSignalConfig()
    version_label = resolved.version_label()
    periods = latest_two_quarters(connection, security_ids, as_of=as_of)

    return {
        security_id: evaluate_institutional_trend(
            security_id=security_id,
            as_of=as_of,
            periods=periods.get(security_id),
            config_version_label=version_label,
        )
        for security_id in security_ids
    }


def store_institutional_signals(
    connection: Connection,
    trends: list[InstitutionalTrend],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Upsert one quarter's trend rows.

    Keyed on `(security_id, year, quarter)` rather than on a date: this
    is quarterly data, and a daily rerun should keep correcting the
    current quarter's row rather than writing ninety identical ones.

    A trend with no period at all — a security with no 13F data — is
    skipped rather than stored: there is no quarter to key it on, and
    `services/intelligence` reads a missing row as exactly the same
    "nothing known" the row would have said.
    """
    storable = [trend for trend in trends if trend.period is not None]
    written = 0
    for start in range(0, len(storable), batch_size):
        batch = storable[start : start + batch_size]
        if not batch:
            continue
        statement = insert(institutional_ownership_signals).values(
            [
                {
                    "security_id": trend.security_id,
                    "year": trend.period.year,
                    "quarter": trend.period.quarter,
                    "investors_holding": trend.investors_holding,
                    "investors_holding_change": trend.investors_holding_change,
                    "total_shares": trend.total_shares,
                    "total_shares_change_percent": trend.total_shares_change_percent,
                    "ownership_percent": trend.ownership_percent,
                    "prior_year": trend.prior_period.year if trend.prior_period else None,
                    "prior_quarter": trend.prior_period.quarter if trend.prior_period else None,
                    "unavailable_reason": (trend.unavailable.value if trend.unavailable else None),
                    "config_version_label": trend.config_version_label,
                    "computed_at": trend.as_of,
                    "detail": trend.detail,
                }
                for trend in batch
            ]
        )
        result = connection.execute(
            statement.on_conflict_do_update(
                constraint="uq_institutional_ownership_signal_period",
                set_={
                    "investors_holding": statement.excluded.investors_holding,
                    "investors_holding_change": statement.excluded.investors_holding_change,
                    "total_shares": statement.excluded.total_shares,
                    "total_shares_change_percent": statement.excluded.total_shares_change_percent,
                    "ownership_percent": statement.excluded.ownership_percent,
                    "prior_year": statement.excluded.prior_year,
                    "prior_quarter": statement.excluded.prior_quarter,
                    "unavailable_reason": statement.excluded.unavailable_reason,
                    "config_version_label": statement.excluded.config_version_label,
                    "computed_at": statement.excluded.computed_at,
                    "detail": statement.excluded.detail,
                },
            ).returning(institutional_ownership_signals.c.id)
        )
        written += len(result.fetchall())
    return written


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

_ONE_DAY = timedelta(days=1)

#: `observation_time` already carries the 45-day filing deadline, so
#: `PitTimestamps.derive` must not apply it a second time on the way to
#: `availability_time`. Zero here means "knowable when observed", which
#: for a 13F is the moment the filing window closes.
_NO_EXTRA_LAG = timedelta(0)


def _period_from(row: Any) -> InstitutionalPeriod:
    payload = dict(row.data or {})
    investors, _ = resolve_field(payload, "investors_holding")
    investors_change, _ = resolve_field(payload, "investors_holding_change")
    shares, _ = resolve_field(payload, "total_shares")
    percent, _ = resolve_field(payload, "ownership_percent")

    return InstitutionalPeriod(
        period=OwnershipQuarter(int(row.year), int(row.quarter)),
        investors_holding=_int(investors),
        investors_holding_change=_int(investors_change),
        total_shares=_decimal(shares),
        ownership_percent=_decimal(percent),
    )


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _plain(value: Decimal | None) -> float | None:
    return None if value is None else float(value)
