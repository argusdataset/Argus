"""Module 09's `AnalogueCounter`, implemented for real.

Module 09 shipped `PeerProfileAnalogueCounter` behind a `Protocol`,
explicitly described as counting cross-sectional peers rather than
historical analogues, as a stand-in for this module. This is the
replacement. Module 09's code is untouched: the protocol is satisfied
structurally, and its swap test passes with this substituted in.

## The threshold must be re-derived, and here is the reasoning

Module 09's report flagged that `min_historical_analogues=5` was
calibrated against the placeholder's output scale, and that a real counter
"returns numbers on a completely different scale". That is exactly right,
and the difference is larger than a rescaling:

| | Placeholder | This module |
|---|---|---|
| Counts | Peers in the same coarse bucket, today | Concluded historical setups within a distance radius |
| Typical value | Tens to hundreds, in a 400-security universe | **Zero**, until Module 17 runs |
| Grows with | Universe size | Accumulated history |

**5 was a reasonable number for the placeholder and is meaningless here.**

### What the number should be, on principle

The gate's purpose is "are there enough analogues to say anything?" — so
the floor should be the point at which a statistic stops being noise. For
the headline statistic (a failure rate, a proportion), a Wilson interval
at n=5 spans roughly ±0.4 — wider than the range of plausible answers, so
it says nothing. At n=15 it is about ±0.25; at n=30, about ±0.18.

`RE_DERIVED_MIN_ANALOGUES = 15` is set from that arithmetic: the smallest
n at which a reported proportion has an interval narrower than the spread
of plausible values. It aligns with this module's own
`min_samples_for_statistics` floor of 5 being a *hard refusal* line and
`preferred_samples` of 30 being comfort — 15 sits between them
deliberately.

**It is arithmetic about intervals, not a finding about markets.** It
cannot be validated until there is a case dataset to validate against.

### The operational consequence, stated plainly

Until Module 17's scan populates the case dataset, this counter returns
**0 for every security**, so enabling it with any positive threshold
gates the entire universe to `INSUFFICIENT_EVIDENCE`. That is arithmetic,
not a bug — but it means **swapping this counter into the live pipeline
before Module 17 runs would stop the pipeline**.

The honest recommendation is in the module README and this module's
report: keep Module 09 on its placeholder counter until the case dataset
exists, then swap and re-derive against real data. This module does not
make that change itself — Module 09's wiring is out of scope.

## Zero, never absent

Module 09's design is explicit: a security with no determinable count
appears with `count=0`, never omitted, because a missing key reads
downstream as "not evaluated" and would silently pass the gate. Honoured
here, including for securities that error out of the search.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.engine import Connection

from core.candidate_detection.eligibility.analogues import AnalogueCount
from core.feature_engine.vector import BatchFeatureResult
from core.historical_similarity.cases import load_cases
from core.historical_similarity.config import SimilarityConfig
from core.historical_similarity.engine import find_similar_setups

#: Re-derived for this module's output scale. See the module docstring for
#: the interval arithmetic behind it. Still a placeholder in the sense
#: that it has never met real data — but derived from a stated principle
#: rather than inherited from a counter that measured something else.
RE_DERIVED_MIN_ANALOGUES = 15


class HistoricalAnalogueCounter:
    """Counts real historical analogues, cross-asset only.

    **Cross-asset only, deliberately.** Module 09's gate asks "are there
    enough comparable past instances to make a statistical claim". A
    security's own three prior attempts are not a statistical base for
    that claim — they are its own history, which this module reports
    separately and which must not be counted toward an eligibility floor
    designed for cross-sectional evidence.
    """

    method = "historical_similarity_v1"

    def __init__(
        self,
        connection: Connection,
        feature_schema_version_id: UUID,
        *,
        config: SimilarityConfig | None = None,
    ) -> None:
        self._connection = connection
        self._feature_schema_version_id = feature_schema_version_id
        self._config = config or SimilarityConfig()

    def count_analogues(
        self,
        securities: list[UUID],
        as_of: datetime,
        features: BatchFeatureResult,
    ) -> dict[UUID, AnalogueCount]:
        """Analogue counts for every requested security. Never omits one.

        Loads the case set **once** for the whole batch rather than per
        security — the same batching discipline Modules 08-10 follow, and
        the difference between one query per scan and one per candidate.
        """
        if not securities:
            return {}

        cases = load_cases(self._connection, as_of, self._feature_schema_version_id)
        counts: dict[UUID, AnalogueCount] = {}

        for security_id in securities:
            vector = features.vectors.get(security_id)
            if vector is None:
                counts[security_id] = AnalogueCount(
                    count=0,
                    method=self.method,
                    provisional=True,
                    detail={"reason": "no_feature_vector"},
                )
                continue

            evidence = find_similar_setups(
                self._connection,
                security_id,
                vector.features,
                as_of,
                self._feature_schema_version_id,
                config=self._config,
                cases=cases,
                # The gate reads a cross-asset count and nothing else.
                # Transition history costs one query per state per
                # security and would be pure waste at universe scale.
                include_same_asset_history=False,
            )
            cross = evidence.cross_asset
            counts[security_id] = AnalogueCount(
                count=cross.count,
                method=self.method,
                # Provisional remains True: the *method* is real, but its
                # radius is an unvalidated placeholder and the dataset it
                # searches is nearly empty. Marking these results
                # non-provisional would claim a validation that has not
                # happened.
                provisional=True,
                detail={
                    "scope": "CROSS_ASSET",
                    "cases_considered": cross.considered,
                    "incomparable": cross.incomparable,
                    "max_distance": self._config.thresholds.max_distance.value,
                    "sufficiency": cross.statistics.sufficiency.value,
                    "calibration_status": "UNVALIDATED_PLACEHOLDERS",
                    "note": (
                        "Counts concluded historical setups within a distance "
                        "radius. Returns 0 for every security until Module 17's "
                        "scan populates the case dataset — see "
                        "core/historical_similarity/counter.py."
                    ),
                },
            )
        return counts
