"""Running all six gates over a candidate pool, in one batch.

Four queries for the whole universe regardless of its size — one universe
membership listing, and three bulk fundamentals loads (plus one more for
the dilution comparison). Everything else is in-memory work over the
feature vectors Module 08 already produced.

That matters for the same reason it mattered in Module 08: full-universe
detection is the primary use case, and a per-security loop over ten
thousand names would issue tens of thousands of queries per scan date,
making a historical replay across fifteen years unrunnable rather than
merely slow.

## Every gate runs, always

No short-circuiting, even once a gate has already failed. It costs
nothing here — the expensive inputs are loaded in bulk up front — and it
is what makes `failures_by_gate()` a usable diagnostic. Knowing a
security failed only liquidity, versus failed liquidity *and* bankruptcy
risk *and* data history, is the difference between "untradeable today"
and "not a real candidate at all".
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.engine import Connection

from core.candidate_detection.config import DetectionConfig
from core.candidate_detection.eligibility.analogues import (
    AnalogueCount,
    AnalogueCounter,
    PeerProfileAnalogueCounter,
)
from core.candidate_detection.eligibility.bankruptcy import (
    evaluate_bankruptcy_gate,
    load_distress_signals,
)
from core.candidate_detection.eligibility.checks import (
    check_data_history,
    check_data_quality,
    check_liquidity,
    check_valid_asset_identity,
)
from core.candidate_detection.eligibility.gates import (
    EligibilityOutcome,
    EligibilityReport,
    GateResult,
)
from core.candidate_detection.pool import CandidatePool
from core.data_validation.result import MissReason
from core.data_validation.universe import MembershipAsOf, list_universe_members_as_of
from core.feature_engine.vector import BatchFeatureResult
from infra.db.enums import EligibilityGate


def evaluate_eligibility(
    connection: Connection,
    pool: CandidatePool,
    features: BatchFeatureResult,
    universe_version_id: UUID,
    *,
    config: DetectionConfig | None = None,
    analogue_counter: AnalogueCounter | None = None,
) -> EligibilityReport:
    """Run all six gates over every candidate in `pool`.

    `analogue_counter` is the Module 11 seam: pass a real implementation
    and the `MINIMUM_HISTORICAL_ANALOGUES` gate becomes a real historical
    check with nothing else in this module touched. Defaults to the
    provisional peer-profile counter, which is explicit about what it
    actually measures.

    `as_of` comes from the pool rather than being a separate argument, so
    the eligibility verdict is necessarily evaluated at the same instant
    the features were — passing them independently would let a caller
    accidentally gate today's data against last year's candidates.
    """
    config = config or DetectionConfig()
    counter = analogue_counter or PeerProfileAnalogueCounter()
    as_of = pool.as_of
    candidates = list(pool.candidates)

    if not candidates:
        return EligibilityReport(
            as_of=as_of,
            run_id=pool.run_id,
            detection_configuration_id=pool.detection_configuration_id,
        )

    memberships = _memberships(connection, universe_version_id, as_of, candidates)
    distress = load_distress_signals(connection, candidates, as_of)
    analogues = counter.count_analogues(candidates, as_of, features)

    outcomes: dict[UUID, EligibilityOutcome] = {}
    for security_id in candidates:
        vector = features.vectors.get(security_id)
        if vector is None:
            # A candidate with no vector cannot occur via `detect_candidates`,
            # which builds the pool from these same vectors. Handled anyway
            # so a hand-assembled pool fails every gate loudly rather than
            # raising a KeyError halfway through a universe scan.
            outcomes[security_id] = _all_gates_unevaluable(security_id, as_of)
            continue

        results = {
            EligibilityGate.DATA_HISTORY: check_data_history(vector, config.eligibility),
            EligibilityGate.DATA_QUALITY: check_data_quality(vector, config.eligibility),
            EligibilityGate.LIQUIDITY: check_liquidity(vector, config.eligibility),
            EligibilityGate.BANKRUPTCY_RISK: evaluate_bankruptcy_gate(
                distress[security_id], config.eligibility
            ),
            EligibilityGate.MINIMUM_HISTORICAL_ANALOGUES: _analogue_gate(
                analogues.get(security_id), config
            ),
            EligibilityGate.VALID_ASSET_IDENTITY: check_valid_asset_identity(
                security_id,
                memberships.get(security_id),
                as_of,
                miss_reason=None if security_id in memberships else MissReason.OUTSIDE_INTERVAL,
            ),
        }
        outcomes[security_id] = EligibilityOutcome(
            security_id=security_id, as_of=as_of, results=results
        )

    return EligibilityReport(
        as_of=as_of,
        run_id=pool.run_id,
        detection_configuration_id=pool.detection_configuration_id,
        outcomes=outcomes,
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _memberships(
    connection: Connection,
    universe_version_id: UUID,
    as_of: datetime,
    candidates: list[UUID],
) -> dict[UUID, MembershipAsOf]:
    """Universe membership for the batch, in one query.

    `list_universe_members_as_of` returns every member at `as_of`; the
    candidate set is intersected in memory rather than issuing one
    `get_universe_membership_as_of` per security. Same PIT predicate —
    Module 06's half-open listing interval — via the same Module 07
    function, just asked once.
    """
    wanted = set(candidates)
    return {
        membership.security_id: membership
        for membership in list_universe_members_as_of(connection, universe_version_id, as_of)
        if membership.security_id in wanted
    }


def _analogue_gate(count: AnalogueCount | None, config: DetectionConfig) -> GateResult:
    """Wrap an analogue count as a gate result.

    A counter that omitted a security is treated as zero analogues and
    said so, rather than passing it: an absent count is not evidence that
    enough analogues exist.
    """
    minimum = config.eligibility.min_historical_analogues
    if count is None:
        return GateResult(
            gate=EligibilityGate.MINIMUM_HISTORICAL_ANALOGUES,
            passed=False,
            detail={
                "analogue_count": 0,
                "min_historical_analogues": minimum,
                "reason": "counter_returned_no_entry",
            },
        )
    return GateResult(
        gate=EligibilityGate.MINIMUM_HISTORICAL_ANALOGUES,
        passed=count.count >= minimum,
        detail={
            "analogue_count": count.count,
            "min_historical_analogues": minimum,
            # Carried so a row recorded before Module 11 exists stays
            # distinguishable from one recorded after.
            "method": count.method,
            "provisional": count.provisional,
            **(count.detail or {}),
        },
    )


def _all_gates_unevaluable(security_id: UUID, as_of: datetime) -> EligibilityOutcome:
    """Every gate failed, with the reason named. Never a silent pass."""
    detail = {"reason": "no_feature_vector_for_candidate"}
    return EligibilityOutcome(
        security_id=security_id,
        as_of=as_of,
        results={
            gate: GateResult(gate=gate, passed=False, detail=dict(detail))
            for gate in EligibilityGate
        },
    )


def new_run_id() -> UUID:
    """A fresh detection-run identifier.

    `eligibility_check_results.run_id` is deliberately not a foreign key —
    Module 03 left "what a detection run is" for this module to decide,
    and it is a grouping token, not an entity.
    """
    return uuid4()
