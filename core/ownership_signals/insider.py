"""The insider-buy cluster signal: did several insiders buy, recently?

## What it counts, and what it refuses to count

Distinct people who made an **open-market purchase** (SEC transaction
code `P`) in the trailing window. Not grants, not option exercises, not
sales — see `config.py` on why compensation arriving on somebody else's
schedule is not a statement of belief.

Distinct *people*, not transactions: an officer who bought in three
tranches across a week is one person who decided once, and counting the
tranches would let a single decision clear a threshold meant to require
agreement between several.

## Three states, and the one that matters

`raised` is `bool | None`, matching `core/risk_context/flags.py` and
`core/news_signals/signal.py`:

- `True` — at least `min_buyers` distinct insiders bought in the window.
- `False` — fewer than that did, measured against real data.
- `None` — this security has no insider-trading history in ARGUS at all,
  so there is nothing to have measured. Reported as `NEVER_INGESTED`.

The `None` case is why this signal is shaped differently from the SEC
filing signal in `core/news_signals/filings.py`, which is a plain `bool`.
A filing is a discrete event that either happened on a given day or did
not; "no cluster of buyers" is a claim about a *window*, and a window
that was never populated has not been looked at. Saying `False` there
would mean "we checked and insiders are not buying" when the truth is
"we have never fetched this security's Form 4 history".

## PIT: the transaction date is not the disclosure date

Form 4 is due within two business days of the trade, so an insider
purchase is not public knowledge on the day it happens. Storage derives
`availability_time` from the transaction date plus
`insider_availability_lag_hours`, and every read here filters on
`availability_time <= as_of` — a replay of last March must not see a
purchase that was still undisclosed then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.data_validation.result import MissReason
from core.ownership_signals.config import (
    OPEN_MARKET_PURCHASE_CODE,
    OwnershipSignalConfig,
    OwnershipThresholds,
)
from data.normalization.persistence import DEFAULT_BATCH_SIZE
from infra.db.schema.ownership_signals import insider_cluster_signals

__all__ = [
    "InsiderCluster",
    "InsiderClusterSignal",
    "assess_insider_batch",
    "evaluate_insider_cluster",
    "insider_purchase_counts",
    "store_insider_signals",
]


@dataclass(frozen=True, slots=True)
class InsiderCluster:
    """The raw counts one security contributed, before any decision."""

    #: Distinct people who made an open-market purchase inside the window.
    distinct_buyers: int
    #: Individual purchase transactions, for the stored detail. Not what
    #: the threshold is applied to — see the module docstring.
    purchase_count: int
    #: Whether ARGUS holds *any* insider transaction for this security,
    #: of any kind, at any date. `False` is what makes a reading
    #: undetermined rather than negative.
    ever_ingested: bool


@dataclass(frozen=True, slots=True)
class InsiderClusterSignal:
    """One security's insider-cluster reading for one day."""

    security_id: UUID
    scan_date: date
    as_of: datetime
    #: None means undetermined. Never coerce it to False.
    raised: bool | None
    distinct_buyers: int
    window_days: int
    min_buyers: int
    config_version_label: str
    #: Set exactly when `raised is None`.
    unavailable: MissReason | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def determined(self) -> bool:
        return self.raised is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "security_id": str(self.security_id),
            "scan_date": self.scan_date.isoformat(),
            "as_of": self.as_of.isoformat(),
            "raised": self.raised,
            "distinct_buyers": self.distinct_buyers,
            "window_days": self.window_days,
            "min_buyers": self.min_buyers,
            "config_version_label": self.config_version_label,
            "unavailable": self.unavailable.value if self.unavailable else None,
            "detail": dict(self.detail),
        }


_PURCHASE_QUERY = """
    SELECT
        security_id,
        COUNT(DISTINCT reporting_person) FILTER (
            WHERE transaction_code = :purchase_code
              AND event_time >= :window_start
              AND event_time < :window_end
              AND reporting_person IS NOT NULL
        ) AS distinct_buyers,
        COUNT(*) FILTER (
            WHERE transaction_code = :purchase_code
              AND event_time >= :window_start
              AND event_time < :window_end
        ) AS purchase_count,
        COUNT(*) AS any_rows
    FROM insider_trades
    WHERE security_id = ANY(:security_ids)
      AND availability_time <= :as_of
    GROUP BY security_id
"""


def insider_purchase_counts(
    connection: Connection,
    security_ids: list[UUID],
    *,
    scan_date: date,
    as_of: datetime,
    thresholds: OwnershipThresholds,
) -> dict[UUID, InsiderCluster]:
    """Distinct open-market buyers per security, in one query.

    One query for however many securities, never one per security — the
    same discipline every batched reader in this project follows.

    `any_rows` is counted alongside so the caller can tell "no buyers in
    the window" from "no insider data at all", which is the entire
    difference between `raised=False` and `raised=None`. It counts rows
    knowable at `as_of`, not rows in the table: a security whose only
    filings are still inside their two-day disclosure window has, as of
    that instant, nothing ARGUS could have seen.
    """
    if not security_ids:
        return {}

    # The window is the `cluster_window_days` before `scan_date`, plus
    # `scan_date` itself: a purchase disclosed this morning is part of
    # today's reading, not tomorrow's.
    day_start = datetime.combine(scan_date, time.min, tzinfo=UTC)
    rows = connection.execute(
        text(_PURCHASE_QUERY),
        {
            "security_ids": security_ids,
            "as_of": as_of,
            "purchase_code": OPEN_MARKET_PURCHASE_CODE,
            "window_start": day_start - thresholds.cluster_window,
            "window_end": day_start + timedelta(days=1),
        },
    ).all()

    return {
        row.security_id: InsiderCluster(
            distinct_buyers=int(row.distinct_buyers),
            purchase_count=int(row.purchase_count),
            ever_ingested=int(row.any_rows) > 0,
        )
        for row in rows
    }


def evaluate_insider_cluster(
    *,
    security_id: UUID,
    scan_date: date,
    as_of: datetime,
    cluster: InsiderCluster | None,
    thresholds: OwnershipThresholds,
    config_version_label: str,
) -> InsiderClusterSignal:
    """The three-state decision, from counts a caller already fetched.

    `cluster=None` — the security is absent from the query's `GROUP BY`,
    meaning ARGUS holds no insider transaction for it that was knowable at
    `as_of` — is `NEVER_INGESTED`, not a measured absence of buying.
    """
    window_days = thresholds.cluster_window_days
    min_buyers = thresholds.min_buyers
    buyers = cluster.distinct_buyers if cluster else 0

    detail: dict[str, Any] = {
        "distinct_buyers": buyers,
        "purchase_count": cluster.purchase_count if cluster else 0,
        "window_days": window_days,
        "min_buyers": min_buyers,
        "transaction_code_counted": OPEN_MARKET_PURCHASE_CODE,
        "scan_date": scan_date.isoformat(),
    }

    if cluster is None or not cluster.ever_ingested:
        return InsiderClusterSignal(
            security_id=security_id,
            scan_date=scan_date,
            as_of=as_of,
            raised=None,
            distinct_buyers=0,
            window_days=window_days,
            min_buyers=min_buyers,
            config_version_label=config_version_label,
            unavailable=MissReason.NEVER_INGESTED,
            detail={
                "reason": (
                    "No insider transaction knowable at this cutoff has ever been "
                    "recorded for this security, so there is nothing to have measured."
                ),
                **detail,
            },
        )

    return InsiderClusterSignal(
        security_id=security_id,
        scan_date=scan_date,
        as_of=as_of,
        raised=buyers >= min_buyers,
        distinct_buyers=buyers,
        window_days=window_days,
        min_buyers=min_buyers,
        config_version_label=config_version_label,
        detail=detail,
    )


def assess_insider_batch(
    connection: Connection,
    security_ids: list[UUID],
    *,
    scan_date: date,
    as_of: datetime,
    config: OwnershipSignalConfig | None = None,
) -> dict[UUID, InsiderClusterSignal]:
    """Every security's insider reading for `scan_date`, from one query."""
    if not security_ids:
        return {}

    resolved = config or OwnershipSignalConfig()
    version_label = resolved.version_label()
    counts = insider_purchase_counts(
        connection,
        security_ids,
        scan_date=scan_date,
        as_of=as_of,
        thresholds=resolved.thresholds,
    )

    return {
        security_id: evaluate_insider_cluster(
            security_id=security_id,
            scan_date=scan_date,
            as_of=as_of,
            cluster=counts.get(security_id),
            thresholds=resolved.thresholds,
            config_version_label=version_label,
        )
        for security_id in security_ids
    }


def store_insider_signals(
    connection: Connection,
    signals: list[InsiderClusterSignal],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Upsert one day's readings. A same-day rerun overwrites, never doubles."""
    written = 0
    for start in range(0, len(signals), batch_size):
        batch = signals[start : start + batch_size]
        if not batch:
            continue
        statement = insert(insider_cluster_signals).values(
            [
                {
                    "security_id": signal.security_id,
                    "signal_date": signal.scan_date,
                    "raised": signal.raised,
                    "distinct_purchasers": signal.distinct_buyers,
                    "window_days": signal.window_days,
                    "min_insiders": signal.min_buyers,
                    "unavailable_reason": (
                        signal.unavailable.value if signal.unavailable else None
                    ),
                    "config_version_label": signal.config_version_label,
                    "computed_at": signal.as_of,
                    "detail": signal.detail,
                }
                for signal in batch
            ]
        )
        result = connection.execute(
            statement.on_conflict_do_update(
                constraint="uq_insider_cluster_signal_security_date",
                set_={
                    "raised": statement.excluded.raised,
                    "distinct_purchasers": statement.excluded.distinct_purchasers,
                    "window_days": statement.excluded.window_days,
                    "min_insiders": statement.excluded.min_insiders,
                    "unavailable_reason": statement.excluded.unavailable_reason,
                    "config_version_label": statement.excluded.config_version_label,
                    "computed_at": statement.excluded.computed_at,
                    "detail": statement.excluded.detail,
                },
            ).returning(insider_cluster_signals.c.id)
        )
        written += len(result.fetchall())
    return written
