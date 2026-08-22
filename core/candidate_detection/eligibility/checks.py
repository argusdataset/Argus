"""The four gates that read Module 08's evidence directly.

`DATA_HISTORY`, `DATA_QUALITY`, `LIQUIDITY` and `VALID_ASSET_IDENTITY`.
Bankruptcy risk lives in `bankruptcy.py` and analogues in `analogues.py`,
both because they need their own long justifications.

Every gate here reads the `FeatureEvidence` Module 08 attaches to each
vector rather than re-deriving anything. That is the point of Module 08
propagating `MissReason` instead of swallowing it: the evidence needed to
decide "can this be judged at all" was already assembled once, honestly,
by the module that knew what was missing.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from core.candidate_detection.config import EligibilityParameters
from core.candidate_detection.eligibility.gates import GateResult
from core.data_validation.result import MissReason
from core.data_validation.universe import MembershipAsOf
from core.feature_engine.vector import FeatureVector
from core.universe.intervals import IntervalEvidence
from infra.db.enums import EligibilityGate, ListingStatus

#: Features without which a candidate cannot be meaningfully judged
#: downstream. Absent sector features are deliberately NOT here — no
#: sector data exists in the schema (Module 08), so requiring them would
#: reject the entire universe for a gap that is nobody's fault.
CRITICAL_FEATURES: tuple[str, ...] = (
    "drawdown_pct",
    "normalized_range_width",
    "realized_volatility",
    "atr",
    "avg_dollar_volume",
)

#: Listing statuses a live candidate may hold. DELISTED and BANKRUPT
#: securities stay in the universe — Module 03's enum comment is explicit
#: that removing them would reintroduce survivorship bias — but they are
#: not tradeable candidates today, which is a separate decision made here.
TRADEABLE_STATUSES: frozenset[ListingStatus] = frozenset(
    {ListingStatus.LISTED, ListingStatus.SUSPENDED}
)


def check_data_history(vector: FeatureVector, parameters: EligibilityParameters) -> GateResult:
    """Enough real bars to fill the longest window the features need.

    Expressed as a *fraction of what Module 08's spec requires* rather
    than an absolute bar count, so changing a feature window cannot
    silently change what this gate means. `bars_required` comes from the
    spec itself; hardcoding 252 here would let the two drift apart with
    nothing to catch it.
    """
    evidence = vector.evidence
    coverage = evidence.coverage_ratio
    return GateResult(
        gate=EligibilityGate.DATA_HISTORY,
        passed=coverage >= parameters.min_history_coverage,
        detail={
            "bars_available": evidence.bars_available,
            "bars_required": evidence.bars_required,
            "coverage_ratio": coverage,
            "min_history_coverage": parameters.min_history_coverage,
        },
    )


def check_data_quality(vector: FeatureVector, parameters: EligibilityParameters) -> GateResult:
    """Enough of the feature set actually computed, and nothing critical missing.

    Two conditions, both required:

    - **Completeness.** The fraction of features that produced a number.
      A candidate assembled mostly from absences is not a candidate.
    - **Critical features present.** Completeness alone is not enough:
      a vector could clear 70% while missing precisely the measurements
      every downstream module depends on. A gate that averaged over that
      would pass a security with no computable drawdown.

    The `MissReason` values Module 08 recorded travel into `detail`
    verbatim, so a rejection names the input that was absent and why —
    which is what makes `INSUFFICIENT_EVIDENCE` a diagnosis rather than a
    shrug.
    """
    evidence = vector.evidence
    total = len(vector.features)
    computed = len(vector.available_features())
    completeness = computed / total if total else 0.0

    missing_critical = [name for name in CRITICAL_FEATURES if vector.features.get(name) is None]

    passed = completeness >= parameters.min_feature_completeness and not missing_critical
    return GateResult(
        gate=EligibilityGate.DATA_QUALITY,
        passed=passed,
        detail={
            "features_computed": computed,
            "features_total": total,
            "completeness": completeness,
            "min_feature_completeness": parameters.min_feature_completeness,
            "missing_critical_features": missing_critical,
            "missing_inputs": {
                name: reason.value for name, reason in evidence.missing_inputs.items()
            },
        },
    )


def check_liquidity(vector: FeatureVector, parameters: EligibilityParameters) -> GateResult:
    """Average dollar volume above an execution-feasibility floor.

    ## Why this floor is absolute when everything else here is relative

    "Can a position actually be filled" is a question about dollars, and
    dollars do not rank cross-sectionally. A percentile floor would
    exclude the bottom N% of the universe every single day by
    construction, however tradeable that slice actually was.

    ## Why the floor is set where it is

    ARGUS exists to find names during their basing periods, and the
    motivating real examples — MLSS, SLS, HIVE, ALX, QBTS — were several
    of them thinly traded for exactly the stretch ARGUS would want to
    have caught them in. A floor chosen for respectability rather than
    feasibility would silently delete the target population and look
    disciplined doing it.

    So the floor answers only: *is a retail-scale position possible
    without the position becoming the tape?* At $50,000 of average daily
    dollar volume, a $2,500 position is 5% of a day's turnover — small
    enough to work into over a session. Below that, even modest size
    dominates the book, and the security is excluded on execution
    grounds rather than on any judgement about the company.

    Module 08 computes `avg_dollar_volume` from **raw** close times
    volume, not the split-adjusted series, which matters here: adjusted
    prices would understate historical turnover by the cumulative split
    factor and this gate would reject securities that were perfectly
    liquid at the time.

    A vector with no `avg_dollar_volume` at all fails, rather than
    passing on the benefit of the doubt — an unmeasurable liquidity is
    not evidence of adequate liquidity.
    """
    value = vector.features.get("avg_dollar_volume")
    passed = value is not None and value >= parameters.min_avg_dollar_volume
    return GateResult(
        gate=EligibilityGate.LIQUIDITY,
        passed=passed,
        detail={
            "avg_dollar_volume": value,
            "min_avg_dollar_volume": parameters.min_avg_dollar_volume,
            "measurable": value is not None,
        },
    )


def check_valid_asset_identity(
    security_id: UUID,
    membership: MembershipAsOf | None,
    as_of: datetime,
    miss_reason: MissReason | None = None,
) -> GateResult:
    """Resolves cleanly through Module 06's universe machinery at `as_of`.

    Three ways to fail, kept distinct in `detail` because they mean
    different things: no membership row covers this date (the security
    was not listed then), the listing status is not tradeable, or the
    interval boundaries rest on weak evidence.

    The evidence check is the subtle one. Module 06 ranks how a listing
    interval's boundaries were established — `delisted_feed` >
    `price_history` > `first_observed` > `missing`. A `missing` boundary
    means ARGUS does not actually know when the security was listed, so
    "was it listed at `as_of`" is being answered by an interval that was
    guessed. That is not a valid identity resolution, and treating it as
    one would let survivorship assumptions in through the back door.
    `first_observed` is weak but real evidence and is allowed through,
    with the fact recorded.
    """
    if membership is None:
        return GateResult(
            gate=EligibilityGate.VALID_ASSET_IDENTITY,
            passed=False,
            detail={
                "resolved": False,
                "reason": (miss_reason or MissReason.OUTSIDE_INTERVAL).value,
                "as_of": as_of.isoformat(),
            },
        )

    tradeable = membership.listing_status in TRADEABLE_STATUSES
    boundaries_known = (
        membership.from_evidence is not IntervalEvidence.MISSING
        and membership.to_evidence is not IntervalEvidence.MISSING
    )

    return GateResult(
        gate=EligibilityGate.VALID_ASSET_IDENTITY,
        passed=tradeable and boundaries_known,
        detail={
            "resolved": True,
            "listing_status": membership.listing_status.value,
            "tradeable_status": tradeable,
            "from_evidence": membership.from_evidence.value,
            "to_evidence": membership.to_evidence.value,
            "boundaries_known": boundaries_known,
            "universe_version_id": str(membership.universe_version_id),
        },
    )
