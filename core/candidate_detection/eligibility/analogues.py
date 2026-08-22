"""The `MINIMUM_HISTORICAL_ANALOGUES` gate — and the seam Module 11 fills.

## The forward dependency, and how it is contained

The gate asks: *are there enough comparable past instances to make any
claim about this setup at all?* Answering it properly means searching
ARGUS's history for structurally similar situations, which is Module 11's
entire job. Module 11 does not exist yet.

Rather than defer the gate (leaving a hole in the eligibility contract) or
build a similarity engine here (scope creep into another module, and a
second implementation to reconcile later), this module defines the
`AnalogueCounter` protocol and ships one deliberately weak implementation
behind it. Module 11 supplies a real one; nothing else in this package
changes.

The seam is a protocol rather than a subclass hierarchy so Module 11 owes
this module nothing — no import, no base class, no registration. It has
to satisfy a three-argument call and return counts. That is the whole
contract, and `tests/.../test_analogue_swap.py` exercises it with a stub
that shares no code with the shipped implementation.

## What the provisional implementation actually measures — read this

`PeerProfileAnalogueCounter` counts **cross-sectional peers at a single
`as_of`**, not historical analogues. It asks "how many other securities
look coarsely like this one *right now*", which is a genuinely different
question from "how many times has something like this happened before".

That is not a shortcut being quietly taken; it is the honest limit of
what is computable from one batch of Module 08 feature vectors, which is
all this module has. Counting real historical analogues needs features
across many dates and a similarity metric, and building either here would
be exactly the Module 11 work the boundaries forbid.

So the count is reported with `method="peer_profile"` and
`provisional=True`, both stored in the gate's `detail`, so that:

- nobody downstream mistakes it for a historical count;
- every gate result recorded before Module 11 exists is identifiable
  afterwards, and can be re-run rather than silently trusted.

**What it can and cannot do.** It can catch a genuinely unique
profile — a security whose feature combination nothing else in the
universe resembles, where any downstream statistical claim would rest on
a sample of one. It cannot say anything about whether this *pattern* has
historically resolved well, badly, or at all. A gate result from this
implementation means "this is not structurally unprecedented among its
peers today", and nothing stronger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import numpy as np
import pandas as pd

from core.feature_engine.vector import BatchFeatureResult

#: Features the coarse profile is bucketed on. Few and structural on
#: purpose: a fine-grained profile would find zero analogues for
#: everything and the gate would reject the entire universe.
PROFILE_FEATURES: tuple[str, ...] = (
    "drawdown_pct",
    "volatility_compression",
    "normalized_range_width",
)

#: Quantile buckets per feature. Coarse — this is a "roughly like this"
#: comparison, and pretending to more precision than the provisional
#: method has would be the opposite of the point.
PROFILE_BUCKETS = 4


@dataclass(frozen=True, slots=True)
class AnalogueCount:
    """How many analogues were found, and by what method.

    `method` and `provisional` are carried into the stored gate `detail`
    rather than being implementation trivia: a result produced by the
    placeholder and a result produced by Module 11 answer different
    questions, and a stored row that could not distinguish them would be
    unauditable the moment Module 11 lands.
    """

    count: int
    method: str
    provisional: bool
    #: Anything the method wants to expose for explainability.
    detail: dict[str, Any] | None = None


@runtime_checkable
class AnalogueCounter(Protocol):
    """The seam. Module 11 implements this and nothing else changes."""

    def count_analogues(
        self,
        securities: list[UUID],
        as_of: datetime,
        features: BatchFeatureResult,
    ) -> dict[UUID, AnalogueCount]:
        """Analogue counts for `securities`, as knowable at `as_of`.

        `as_of` is a plain argument, per
        `docs/architecture/CROSS_CUTTING_REQUIREMENTS.md` — a real
        implementation must not consult data unavailable at that instant,
        and a historical replay must be the same call with a different
        value. A security with no determinable count must appear in the
        result with `count=0`, never be omitted: a missing key would read
        downstream as "not evaluated" and silently pass the gate.
        """
        ...


class PeerProfileAnalogueCounter:
    """Provisional counter: cross-sectional peers sharing a coarse profile.

    Vectorized — one bucketing pass over the batch, then a group count.
    Sized to be replaced, not extended.
    """

    method = "peer_profile"

    def __init__(self, *, buckets: int = PROFILE_BUCKETS) -> None:
        self._buckets = buckets

    def count_analogues(
        self,
        securities: list[UUID],
        as_of: datetime,
        features: BatchFeatureResult,
    ) -> dict[UUID, AnalogueCount]:
        """Peers in the same bucket, excluding the security itself."""
        frame = self._profile_frame(features)
        if frame.empty:
            return {
                security_id: AnalogueCount(0, self.method, True, {"reason": "no_features"})
                for security_id in securities
            }

        bucketed = frame.apply(self._bucket, axis=0)
        # A security missing any profile feature cannot be placed in a
        # bucket at all; it gets a count of zero with the reason recorded,
        # rather than being grouped with everything else that is also NaN.
        complete = bucketed.notna().all(axis=1)
        keys = bucketed[complete].astype(int).astype(str).agg("|".join, axis=1)
        group_sizes = keys.value_counts()

        counts: dict[UUID, AnalogueCount] = {}
        for security_id in securities:
            if security_id not in keys.index:
                counts[security_id] = AnalogueCount(
                    0, self.method, True, {"reason": "incomplete_profile"}
                )
                continue
            key = keys.loc[security_id]
            peers = int(group_sizes.get(key, 1)) - 1  # exclude self
            counts[security_id] = AnalogueCount(
                count=max(0, peers),
                method=self.method,
                provisional=True,
                detail={"profile_bucket": key, "population": int(len(keys))},
            )
        return counts

    def _profile_frame(self, features: BatchFeatureResult) -> pd.DataFrame:
        if not features.vectors:
            return pd.DataFrame(columns=list(PROFILE_FEATURES))
        rows = {
            security_id: {name: vector.features.get(name) for name in PROFILE_FEATURES}
            for security_id, vector in features.vectors.items()
        }
        return pd.DataFrame.from_dict(rows, orient="index", columns=list(PROFILE_FEATURES)).astype(
            float
        )

    def _bucket(self, column: pd.Series) -> pd.Series:
        """Quantile buckets, tolerating columns too degenerate to cut.

        `qcut` raises when a column has fewer distinct values than
        buckets — common in a small batch, and not an error: it just means
        every security shares that feature's bucket.
        """
        if column.notna().sum() == 0:
            return pd.Series(np.nan, index=column.index)
        try:
            return pd.qcut(column, self._buckets, labels=False, duplicates="drop")
        except ValueError:
            return pd.Series(np.where(column.notna(), 0, np.nan), index=column.index)
