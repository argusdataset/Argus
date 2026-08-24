"""The frame every metric is computed from: one row per evaluated setup.

## Why the join is the interesting part

An evaluation row needs three things that live in three tables: what ARGUS
predicted (`signals`), what it committed to (`setups`), and what actually
happened (`setup_outcomes`). Before migration 0007 the first two could
only be connected through a copy of the score in a lifecycle event's JSONB
payload — Module 15 did exactly that and said in its report that this
module would need a real join key. It does, and 0007 provides it:
`setups.qualifying_signal_id`.

That column being NULL is not missing data. It means the setup was
detected and never qualified — ARGUS noticed a base and did not commit to
it — and those rows are essential rather than incidental: they are the
denominator of recall and the source of every true negative. Dropping
them would produce a system that can only be measured on the cases it
already believed in.

## Point-in-time correctness

`as_of` bounds the outcomes: only outcomes recorded by then are visible,
the same cutoff Module 11's case loader applies and for the same reason.
An evaluation that read outcomes recorded after its own `as_of` would
score the model against knowledge it did not have.

`data_snapshot_id` selects **which labelling** to evaluate against. Since
migration 0007 a setup may carry several outcome rows, one per snapshot,
each computed under a different success criterion. Passing None takes the
most recently recorded per setup — the labelling ARGUS currently believes
— and the column travels on every row so a report can state which one it
used rather than leaving it to be assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

#: One row per setup. Every column is either a prediction, a commitment,
#: or an outcome — nothing derived, so a metric that wants a derived
#: quantity has to compute it where it is read.
EVALUATION_COLUMNS: tuple[str, ...] = (
    "setup_id",
    "security_id",
    "detected_at",
    "concluded_at",
    "qualifying_signal_id",
    "argus_score",
    "confidence",
    "opportunity_score",
    "risk_score",
    "signal_event_time",
    "outcome_status",
    "mfe",
    "mae",
    "realized_return",
    "benchmark_relative_return",
    "volatility_adjusted_outcome",
    "time_to_mfe",
    "outcome_window",
    "market_regime_at_outcome",
    "review_confidence",
    "false_positive_type",
    "recorded_at",
    "data_snapshot_id",
    "target_model_version_id",
    "universe_version_id",
)

_DATASET_QUERY = """
    SELECT DISTINCT ON (s.id)
           s.id                          AS setup_id,
           s.security_id                 AS security_id,
           s.detected_at                 AS detected_at,
           s.concluded_at                AS concluded_at,
           s.qualifying_signal_id        AS qualifying_signal_id,
           s.target_model_version_id     AS target_model_version_id,
           s.universe_version_id         AS universe_version_id,
           sig.argus_score               AS argus_score,
           sig.confidence                AS confidence,
           sig.opportunity_score         AS opportunity_score,
           sig.risk_score                AS risk_score,
           sig.event_time                AS signal_event_time,
           o.outcome_status              AS outcome_status,
           o.mfe                         AS mfe,
           o.mae                         AS mae,
           o.realized_return             AS realized_return,
           o.benchmark_relative_return   AS benchmark_relative_return,
           o.volatility_adjusted_outcome AS volatility_adjusted_outcome,
           o.time_to_mfe                 AS time_to_mfe,
           o.outcome_window              AS outcome_window,
           o.market_regime_at_outcome    AS market_regime_at_outcome,
           o.review_confidence           AS review_confidence,
           o.false_positive_type         AS false_positive_type,
           o.recorded_at                 AS recorded_at,
           o.data_snapshot_id            AS data_snapshot_id
    FROM setups s
    JOIN setup_outcomes o ON o.setup_id = s.id
    LEFT JOIN signals sig ON sig.id = s.qualifying_signal_id
    WHERE o.recorded_at <= :as_of
      {snapshot_clause}
      {period_clause}
      {run_clause}
    ORDER BY s.id, o.recorded_at DESC, o.id DESC
"""


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    """Evaluated setups, and what the load was bounded by.

    Carries its own bounds so a report can state what it measured rather
    than describing a frame whose provenance the reader has to trust.
    """

    frame: pd.DataFrame
    as_of: datetime
    period_start: datetime | None = None
    period_end: datetime | None = None
    data_snapshot_id: UUID | None = None

    def __len__(self) -> int:
        return len(self.frame)

    @property
    def is_empty(self) -> bool:
        return self.frame.empty

    @property
    def qualified(self) -> pd.DataFrame:
        """Setups ARGUS committed to — where a score exists."""
        if self.frame.empty:
            return self.frame
        return self.frame[self.frame["qualifying_signal_id"].notna()]

    @property
    def unqualified(self) -> pd.DataFrame:
        """Setups detected and never qualified. Recall's other half."""
        if self.frame.empty:
            return self.frame
        return self.frame[self.frame["qualifying_signal_id"].isna()]

    def bounds(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "data_snapshot_id": str(self.data_snapshot_id) if self.data_snapshot_id else None,
            "setups": len(self.frame),
            "qualified": len(self.qualified),
        }

    def within(self, start: datetime, end: datetime) -> EvaluationDataset:
        """The same dataset narrowed to setups detected in `[start, end]`.

        Used by walk-forward, which needs many overlapping views of one
        load rather than one query per window — at real scale that is the
        difference between one pass over the dataset and dozens.
        """
        if self.frame.empty:
            return EvaluationDataset(
                frame=self.frame,
                as_of=self.as_of,
                period_start=start,
                period_end=end,
                data_snapshot_id=self.data_snapshot_id,
            )
        detected = self.frame["detected_at"]
        mask = (detected >= start) & (detected <= end)
        return EvaluationDataset(
            frame=self.frame[mask],
            as_of=self.as_of,
            period_start=start,
            period_end=end,
            data_snapshot_id=self.data_snapshot_id,
        )


def load_evaluation_dataset(
    connection: Connection,
    *,
    as_of: datetime,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    data_snapshot_id: UUID | None = None,
    universe_version_id: UUID | None = None,
) -> EvaluationDataset:
    """Every concluded setup knowable at `as_of`, with its score and outcome.

    One query for the whole dataset. Deliberately not one per setup or per
    window: at ~10,000 securities over 15 years this frame is the thing
    every metric in this module reads, and it is loaded once.
    """
    params: dict[str, Any] = {"as_of": as_of}

    snapshot_clause = ""
    if data_snapshot_id is not None:
        snapshot_clause = "AND o.data_snapshot_id = :snapshot"
        params["snapshot"] = data_snapshot_id

    period_clause = ""
    if period_start is not None:
        period_clause += " AND s.detected_at >= :period_start"
        params["period_start"] = period_start
    if period_end is not None:
        period_clause += " AND s.detected_at <= :period_end"
        params["period_end"] = period_end

    run_clause = ""
    if universe_version_id is not None:
        run_clause = "AND s.universe_version_id = :universe_version_id"
        params["universe_version_id"] = universe_version_id

    rows = connection.execute(
        text(
            _DATASET_QUERY.format(
                snapshot_clause=snapshot_clause,
                period_clause=period_clause,
                run_clause=run_clause,
            )
        ),
        params,
    ).all()

    frame = pd.DataFrame(
        [{name: getattr(row, name) for name in EVALUATION_COLUMNS} for row in rows],
        columns=list(EVALUATION_COLUMNS),
    )
    return EvaluationDataset(
        frame=_typed(frame),
        as_of=as_of,
        period_start=period_start,
        period_end=period_end,
        data_snapshot_id=data_snapshot_id,
    )


def _typed(frame: pd.DataFrame) -> pd.DataFrame:
    """Numerics as floats, enums as strings, timestamps as timestamps.

    Postgres returns `Numeric` as `Decimal`, which pandas stores as an
    object column — every subsequent `.mean()` would then work but return
    a Decimal, and a Decimal compared against a float threshold raises
    rather than being wrong quietly. Converting once here is why nothing
    downstream has to think about it.
    """
    if frame.empty:
        return frame
    numeric = (
        "argus_score",
        "confidence",
        "opportunity_score",
        "risk_score",
        "mfe",
        "mae",
        "realized_return",
        "benchmark_relative_return",
        "volatility_adjusted_outcome",
    )
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("detected_at", "concluded_at", "recorded_at", "signal_event_time"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    return frame
