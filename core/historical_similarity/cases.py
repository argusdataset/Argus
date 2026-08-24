"""Loading the historical CASE dataset, PIT-correctly.

A **case** is a completed historical setup: a `setups` row that reached an
outcome (`setup_outcomes`), joined to the feature vector Module 08
computed for that security at the moment it was detected. Without all
three parts it is not a case — a setup with no outcome has nothing to say
about what happens next, and one with no feature vector cannot be compared
to anything.

## The dataset is nearly empty, and this module is built for that

Module 17's full historical scan has not run. Until it does, there are
approximately zero cases, and every query returns INSUFFICIENT. That is
the correct behaviour, not a degraded one — see `statistics.py`.

Building as though a populated dataset existed would produce a module that
looks fine in tests and reports confident statistics from three cases the
day it meets real data.

## Point-in-time correctness

Two separate cutoffs, and conflating them is the leak this module could
introduce:

1. **A case must have *concluded* before `as_of`.** Its outcome must have
   been recorded by then. Comparing today's candidate against a setup
   whose outcome is still unfolding would let the future inform the
   present — the exact failure Module 07 exists to prevent, arriving
   through a table Module 07's `get_as_of` never sees.

2. **The case's feature vector must be the one knowable at *its own*
   detection**, filtered on `availability_time <= detected_at`. Using the
   vector as it looks today would compare against a version of history
   that includes later restatements.

Both are enforced in SQL rather than in Python, so a caller cannot forget
one.

## Which outcome row wins

Migration 0007 changed `setup_outcomes` from one row per setup to one row
per setup **per data snapshot**, so that an outcome can be recomputed when
the success criterion is revised. This query's `DISTINCT ON (s.id)`
previously leaned on the old uniqueness for determinism — it ordered only
the feature vector, because the outcome could not be ambiguous.

It can now, so the ordering names the outcome first: the most recently
recorded one wins, with `o.id` breaking an exact `recorded_at` tie so the
result is stable rather than merely usually stable. That is the right
default for a similarity lookup — the latest labelling of history is the
one ARGUS currently believes — and a caller wanting a specific snapshot's
labelling wants a different query, not a different tiebreak.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

#: Columns every case carries, beyond its features.
CASE_COLUMNS = (
    "setup_id",
    "security_id",
    "detected_at",
    "outcome_status",
    "mfe",
    "mae",
    "realized_return",
    "benchmark_relative_return",
    "time_to_mfe",
    "outcome_window",
    "market_regime_at_outcome",
    "recorded_at",
)

#: One query for the whole case set. `DISTINCT ON` picks, per setup, the
#: feature vector that was knowable at that setup's own detection —
#: cutoff (2) in the module docstring — while the outer WHERE enforces
#: cutoff (1), that the outcome had been recorded by `as_of`.
_CASE_QUERY = """
    SELECT DISTINCT ON (s.id)
           s.id                        AS setup_id,
           s.security_id               AS security_id,
           s.detected_at               AS detected_at,
           o.outcome_status            AS outcome_status,
           o.mfe                       AS mfe,
           o.mae                       AS mae,
           o.realized_return           AS realized_return,
           o.benchmark_relative_return AS benchmark_relative_return,
           o.time_to_mfe               AS time_to_mfe,
           o.outcome_window            AS outcome_window,
           o.market_regime_at_outcome  AS market_regime_at_outcome,
           o.recorded_at               AS recorded_at,
           fv.features                 AS features
    FROM setups s
    JOIN setup_outcomes o ON o.setup_id = s.id
    JOIN feature_vectors fv
      ON fv.security_id = s.security_id
     AND fv.feature_schema_version_id = :feature_schema_version_id
     AND fv.availability_time <= s.detected_at
    WHERE o.recorded_at <= :as_of
      {security_clause}
    ORDER BY s.id,
             o.recorded_at DESC, o.id DESC,
             fv.event_time DESC, fv.availability_time DESC
"""


@dataclass(frozen=True, slots=True)
class CaseSet:
    """Historical cases, split into their feature and outcome halves.

    Kept as one object because the two frames share an index (`setup_id`)
    and separating them at the call site would invite them to drift out of
    alignment.
    """

    #: (setup_id x feature name), the comparison space.
    features: pd.DataFrame
    #: (setup_id x outcome columns), what actually happened.
    outcomes: pd.DataFrame

    def __len__(self) -> int:
        return len(self.features)

    @property
    def is_empty(self) -> bool:
        return self.features.empty

    def excluding_security(self, security_id: UUID) -> CaseSet:
        """Every case except this security's own — the cross-asset set.

        The single most important filter in this module. Leaving a
        security's own history in its cross-asset comparison would let it
        corroborate itself, and the cross-asset/same-asset separation
        exists precisely because those are different kinds of evidence.
        """
        keep = self.outcomes["security_id"] != security_id
        return CaseSet(features=self.features[keep.to_numpy()], outcomes=self.outcomes[keep])

    def only_security(self, security_id: UUID) -> CaseSet:
        """Only this security's own cases — the same-asset set."""
        keep = self.outcomes["security_id"] == security_id
        return CaseSet(features=self.features[keep.to_numpy()], outcomes=self.outcomes[keep])


def load_cases(
    connection: Connection,
    as_of: datetime,
    feature_schema_version_id: UUID,
    *,
    security_ids: list[UUID] | None = None,
) -> CaseSet:
    """Every case concluded by `as_of`, with its detection-time features.

    One query regardless of dataset size. `security_ids` narrows the load
    when only a few securities' histories are wanted (the same-asset
    path); omitted, it loads the whole case set.

    Returns an empty `CaseSet` when nothing has concluded — which, before
    Module 17 runs, is the normal case rather than an error.
    """
    params: dict[str, object] = {
        "as_of": as_of,
        "feature_schema_version_id": str(feature_schema_version_id),
    }
    security_clause = ""
    if security_ids is not None:
        security_clause = "AND s.security_id = ANY(:security_ids)"
        params["security_ids"] = [str(value) for value in security_ids]

    rows = connection.execute(
        text(_CASE_QUERY.format(security_clause=security_clause)), params
    ).all()
    if not rows:
        return CaseSet(features=pd.DataFrame(), outcomes=pd.DataFrame(columns=list(CASE_COLUMNS)))

    outcomes = pd.DataFrame(
        [{name: getattr(row, name) for name in CASE_COLUMNS} for row in rows],
        index=[row.setup_id for row in rows],
    )
    features = pd.DataFrame(
        [numeric_features(row.features) for row in rows],
        index=[row.setup_id for row in rows],
    )
    return CaseSet(features=features, outcomes=outcomes)


def numeric_features(payload: object) -> dict[str, float]:
    """A feature payload as floats, dropping non-numeric entries.

    Module 08 embeds `_evidence` — a nested dict — alongside the features
    in the same JSONB column. It is metadata about the vector, not a
    measurement, and letting it into the comparison space would be
    nonsense. Used on both sides of a comparison, since a caller may hand
    the engine a payload read straight back out of `feature_vectors`.
    """
    if not isinstance(payload, dict):
        return {}
    numeric: dict[str, float] = {}
    for name, value in payload.items():
        if name.startswith("_"):
            continue
        if isinstance(value, int | float) and not isinstance(value, bool):
            numeric[name] = float(value)
    return numeric
