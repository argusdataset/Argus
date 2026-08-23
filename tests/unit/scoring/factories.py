"""Builders for the four upstream evidence objects this module consumes.

Not a test module. Hand-built rather than produced by running Modules 08,
10, 11 and 12 end to end, because a unit test of scoring should fail for
exactly one reason: scoring. The end-to-end coupling is exercised in
`tests/integration/scoring/`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from core.data_validation.result import MissReason
from core.feature_engine.vector import FeatureEvidence, FeatureVector
from core.historical_similarity.engine import (
    SameAssetHistory,
    ScopedEvidence,
    SimilarityEvidence,
)
from core.historical_similarity.statistics import (
    Distribution,
    Interval,
    OutcomeStatistics,
    SampleSufficiency,
)
from core.market_state.target_model_matching.interface import TargetModelAssessment
from core.risk_context.assessment import RiskContext
from core.risk_context.events import EventCoverage, PendingEvent, PendingEventsView
from core.risk_context.flags import (
    EVENT_PROXIMITY,
    LIQUIDITY_DEGREE,
    VOLATILITY_SPIKE,
    RiskFlag,
)
from core.risk_context.invalidation import (
    EligibilityChange,
    EligibilityTrend,
    InvalidationSignals,
)
from core.scoring.components import ScoringInputs
from core.scoring.engine import Lineage
from data.canonical_model.records import CanonicalTimeframe
from infra.db.enums import AnalogueScope

AS_OF = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)

#: A feature vector that makes every feature-driven component measurable.
#: Mid-range values, so a test that moves one feature is unambiguous about
#: which component it moved.
HEALTHY_FEATURES: dict[str, float | None] = {
    "market_regime_trend": 0.0005,
    "market_regime_volatility": 0.18,
    "market_regime_drawdown": -0.05,
    "avg_dollar_volume": 2_000_000.0,
    "spread_proxy": 0.03,
    "volatility_compression": 0.70,
    "atr_percentile": 0.25,
    "normalized_range_width": 0.18,
}


def lineage() -> Lineage:
    return Lineage(
        target_model_version_id=uuid4(),
        feature_schema_version_id=uuid4(),
        data_snapshot_id=uuid4(),
        scoring_configuration_id=uuid4(),
        universe_version_id=uuid4(),
        detection_configuration_id=uuid4(),
    )


def features(
    security_id: UUID,
    values: dict[str, float | None] | None = None,
    *,
    coverage: float = 1.0,
    as_of: datetime = AS_OF,
) -> FeatureVector:
    required = 252
    return FeatureVector(
        security_id=security_id,
        as_of=as_of,
        feature_schema_version_id=None,
        timeframe=CanonicalTimeframe.DAILY,
        event_time=as_of,
        availability_time=as_of,
        features=dict(HEALTHY_FEATURES if values is None else values),
        evidence=FeatureEvidence(bars_available=int(required * coverage), bars_required=required),
    )


def assessment(security_id: UUID, quality: float | None = 0.80) -> TargetModelAssessment:
    return TargetModelAssessment(
        security_id=security_id,
        quality=quality,
        components={"prior_decline": 0.9, "consolidation": 0.7},
        supports_advancement=True,
    )


def statistics(
    *,
    count: int,
    sufficiency: SampleSufficiency,
    failure_rate: float | None = None,
    interval: tuple[float, float] | None = None,
    mfe_mean: float | None = 0.30,
) -> OutcomeStatistics:
    if sufficiency is SampleSufficiency.INSUFFICIENT:
        return OutcomeStatistics(sample_count=count, sufficiency=sufficiency)
    return OutcomeStatistics(
        sample_count=count,
        sufficiency=sufficiency,
        median_outcome=0.12,
        average_outcome=0.15,
        failure_rate=failure_rate,
        failure_rate_interval=(Interval(low=interval[0], high=interval[1]) if interval else None),
        resolved_count=count,
        mfe=(
            None
            if mfe_mean is None
            else Distribution(
                count=count,
                percentiles={50: mfe_mean},
                mean=mfe_mean,
                mean_interval=Interval(low=mfe_mean / 2, high=mfe_mean * 2),
            )
        ),
    )


def similarity(
    security_id: UUID,
    cross: OutcomeStatistics,
    *,
    same: OutcomeStatistics | None = None,
    as_of: datetime = AS_OF,
) -> SimilarityEvidence:
    def scoped(scope: AnalogueScope, stats: OutcomeStatistics) -> ScopedEvidence:
        return ScopedEvidence(
            scope=scope, matches=(), statistics=stats, considered=stats.sample_count
        )

    return SimilarityEvidence(
        security_id=security_id,
        as_of=as_of,
        feature_schema_version_id=uuid4(),
        data_snapshot_id=uuid4(),
        cross_asset=scoped(AnalogueScope.CROSS_ASSET, cross),
        same_asset=scoped(
            AnalogueScope.SAME_ASSET,
            same or OutcomeStatistics(sample_count=0, sufficiency=SampleSufficiency.INSUFFICIENT),
        ),
        same_asset_history=SameAssetHistory(),
    )


def risk_context(
    security_id: UUID,
    *,
    liquidity: float | None = 0.10,
    expansion: float | None = 1.10,
    days_to_event: float | None = 40.0,
    event_coverage: EventCoverage = EventCoverage.KNOWN,
    eligibility_trend: EligibilityTrend = EligibilityTrend.PASSING,
    as_of: datetime = AS_OF,
) -> RiskContext:
    """A Module 12 result. `None` on a degree means the flag is undetermined."""
    events = _events(security_id, event_coverage, days_to_event, as_of)
    return RiskContext(
        security_id=security_id,
        as_of=as_of,
        flags={
            LIQUIDITY_DEGREE: _flag(LIQUIDITY_DEGREE, liquidity),
            VOLATILITY_SPIKE: _flag(VOLATILITY_SPIKE, expansion),
            EVENT_PROXIMITY: _event_flag(events, days_to_event),
        },
        events=events,
        invalidation=InvalidationSignals(
            security_id=security_id,
            as_of=as_of,
            current_state=None,
            backward_transitions=0,
            last_backward_at=None,
            last_transition=None,
            transitions_observed=1,
            eligibility=EligibilityChange(trend=eligibility_trend, runs_observed=2),
        ),
        config_version="test-risk-config",
    )


def scoring_inputs(
    security_id: UUID | None = None,
    *,
    quality: float | None = 0.80,
    cross: OutcomeStatistics | None = None,
    feature_values: dict[str, float | None] | None = None,
    feature_coverage: float = 1.0,
    with_similarity: bool = True,
    with_features: bool = True,
    **risk_kwargs,
) -> ScoringInputs:
    """One candidate's full evidence bundle, with sensible defaults."""
    security_id = security_id or uuid4()
    return ScoringInputs(
        risk=risk_context(security_id, **risk_kwargs),
        features=(
            features(security_id, feature_values, coverage=feature_coverage)
            if with_features
            else None
        ),
        assessment=assessment(security_id, quality),
        similarity=(
            similarity(
                security_id,
                cross or statistics(count=0, sufficiency=SampleSufficiency.INSUFFICIENT),
            )
            if with_similarity
            else None
        ),
    )


def adequate(failure_rate: float = 0.30, *, count: int = 200) -> OutcomeStatistics:
    """A well-supported cross-asset sample with a tight interval."""
    half = 0.06
    return statistics(
        count=count,
        sufficiency=SampleSufficiency.ADEQUATE,
        failure_rate=failure_rate,
        interval=(failure_rate - half, failure_rate + half),
    )


def sparse(failure_rate: float = 0.30, *, count: int = 6) -> OutcomeStatistics:
    """The same point estimate from a handful of cases: a wide interval."""
    half = 0.34
    return statistics(
        count=count,
        sufficiency=SampleSufficiency.SPARSE,
        failure_rate=failure_rate,
        interval=(max(0.0, failure_rate - half), min(1.0, failure_rate + half)),
    )


def insufficient() -> OutcomeStatistics:
    """What every candidate gets until Module 17 populates the case set."""
    return statistics(count=0, sufficiency=SampleSufficiency.INSUFFICIENT)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _flag(name: str, degree: float | None) -> RiskFlag:
    if degree is None:
        return RiskFlag(name=name, raised=None, unavailable=MissReason.NOT_YET_AVAILABLE)
    return RiskFlag(name=name, raised=False, degree=degree)


def _events(
    security_id: UUID,
    coverage: EventCoverage,
    days: float | None,
    as_of: datetime,
) -> PendingEventsView:
    events: tuple[PendingEvent, ...] = ()
    if coverage is EventCoverage.KNOWN and days is not None:
        events = (
            PendingEvent(
                security_id=security_id,
                event_type="EARNINGS",
                scheduled_for=as_of + timedelta(days=days),
                is_binary=True,
                known_from=as_of - timedelta(days=1),
            ),
        )
    return PendingEventsView(
        security_id=security_id,
        as_of=as_of,
        horizon_days=90.0,
        coverage=coverage,
        events=events,
        miss_reason=(MissReason.NEVER_INGESTED if coverage is EventCoverage.UNAVAILABLE else None),
        coverage_observed_at=(
            None if coverage is EventCoverage.UNAVAILABLE else as_of - timedelta(days=1)
        ),
    )


def _event_flag(events: PendingEventsView, days: float | None) -> RiskFlag:
    if events.coverage is EventCoverage.UNAVAILABLE:
        return RiskFlag(name=EVENT_PROXIMITY, raised=None, unavailable=MissReason.NEVER_INGESTED)
    if events.coverage is EventCoverage.NONE_SCHEDULED:
        return RiskFlag(name=EVENT_PROXIMITY, raised=False, degree=None)
    return RiskFlag(name=EVENT_PROXIMITY, raised=days is not None and days <= 14.0, degree=days)
