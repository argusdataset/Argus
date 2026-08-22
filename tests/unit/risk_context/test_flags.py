"""Each flag, in isolation, including when it cannot be determined.

These are unit tests deliberately: a failure here is about one flag's
logic and nothing else. The database-backed behaviour — PIT correctness,
invalidation history — is tested against a real PostgreSQL in
`tests/integration/risk_context/`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from core.data_validation.result import MissReason
from core.risk_context.config import RiskThresholds
from core.risk_context.events import (
    EARNINGS,
    EventCoverage,
    PendingEvent,
    PendingEventsView,
)
from core.risk_context.flags import (
    EVENT_PROXIMITY,
    FEATURE_UNAVAILABLE,
    NO_FEATURE_VECTOR,
    event_proximity_flag,
    liquidity_degree_flag,
    volatility_spike_flag,
)

AS_OF = datetime(2024, 6, 3, 21, 0, tzinfo=UTC)


@pytest.fixture
def thresholds() -> RiskThresholds:
    return RiskThresholds()


def _view(
    *,
    coverage: EventCoverage,
    days: float | None = None,
    miss_reason: MissReason | None = None,
    thresholds: RiskThresholds | None = None,
) -> PendingEventsView:
    thresholds = thresholds or RiskThresholds()
    events: tuple[PendingEvent, ...] = ()
    if days is not None:
        events = (
            PendingEvent(
                security_id=uuid4(),
                event_type=EARNINGS,
                scheduled_for=AS_OF + timedelta(days=days),
                is_binary=True,
                known_from=AS_OF - timedelta(days=1),
            ),
        )
    return PendingEventsView(
        security_id=uuid4(),
        as_of=AS_OF,
        horizon_days=thresholds.event_horizon_days.value,
        coverage=coverage,
        events=events,
        miss_reason=miss_reason,
        coverage_observed_at=AS_OF - timedelta(days=1)
        if coverage is not EventCoverage.UNAVAILABLE
        else None,
    )


# --------------------------------------------------------------------------
# Event proximity — the flag the module exists for
# --------------------------------------------------------------------------


def test_an_imminent_earnings_date_raises_the_proximity_flag(thresholds):
    """The headline case: a structurally sound base with earnings in a week."""
    flag = event_proximity_flag(_view(coverage=EventCoverage.KNOWN, days=7.0), thresholds)

    assert flag.name == EVENT_PROXIMITY
    assert flag.raised is True
    assert flag.degree == pytest.approx(7.0)
    assert flag.detail["is_binary"] is True
    assert flag.detail["event_type"] == EARNINGS


def test_a_distant_earnings_date_does_not_raise_the_flag(thresholds):
    """Inside the horizon, outside the imminence window. Known, not urgent."""
    flag = event_proximity_flag(_view(coverage=EventCoverage.KNOWN, days=60.0), thresholds)

    assert flag.raised is False
    assert flag.degree == pytest.approx(60.0)
    assert flag.unavailable is None


def test_no_event_scheduled_is_a_real_negative_with_no_measured_proximity(thresholds):
    """`raised=False` with `degree=None`.

    Not zero days, and not a large sentinel: there is no event, so there
    is no proximity, and inventing a number for it would be the same
    class of lie as reporting a missing measurement as safe.
    """
    flag = event_proximity_flag(_view(coverage=EventCoverage.NONE_SCHEDULED), thresholds)

    assert flag.raised is False
    assert flag.degree is None
    assert flag.unavailable is None


@pytest.mark.parametrize("reason", [MissReason.NEVER_INGESTED, MissReason.NOT_YET_AVAILABLE])
def test_unavailable_calendar_data_is_never_reported_as_no_event(thresholds, reason):
    """The distinction the Module 12 brief asks for by name.

    A candidate whose calendar was never fetched must not be
    indistinguishable from one that genuinely has nothing scheduled.
    """
    flag = event_proximity_flag(
        _view(coverage=EventCoverage.UNAVAILABLE, miss_reason=reason), thresholds
    )

    assert flag.raised is None
    assert flag.determined is False
    assert flag.unavailable is reason
    assert flag.degree is None


def test_the_imminence_window_is_read_from_the_configuration(thresholds):
    """Move the threshold and the verdict must move — a predicate that
    ignored its config would pass the source scan and still be
    uncalibratable."""
    from core.risk_context.config import CALIBRATABLE, RiskThreshold

    view = _view(coverage=EventCoverage.KNOWN, days=20.0)
    widened = RiskThresholds(
        imminent_event_days=RiskThreshold(value=30.0, kind=CALIBRATABLE, rationale="test override")
    )

    assert event_proximity_flag(view, thresholds).raised is False
    assert event_proximity_flag(view, widened).raised is True


# --------------------------------------------------------------------------
# Liquidity degree
# --------------------------------------------------------------------------


def test_liquidity_degree_is_continuous_above_module_09s_pass_fail_floor(thresholds):
    """Two securities that both cleared the $50,000 gate, ranked apart."""
    thin = liquidity_degree_flag({"avg_dollar_volume": 60_000.0}, thresholds)
    deep = liquidity_degree_flag({"avg_dollar_volume": 5_000_000.0}, thresholds)

    assert thin.raised is True
    assert deep.raised is False
    assert thin.degree > deep.degree
    assert deep.degree == pytest.approx(0.0)


def test_liquidity_degree_is_bounded_and_points_the_same_way_as_the_other_flags(
    thresholds,
):
    """Larger means riskier, everywhere. Module 13 will read these side by side."""
    values = [10_000.0, 100_000.0, 500_000.0, 1_000_000.0, 10_000_000.0]
    degrees = [
        liquidity_degree_flag({"avg_dollar_volume": value}, thresholds).degree for value in values
    ]

    assert degrees == sorted(degrees, reverse=True)
    assert all(0.0 <= degree <= 1.0 for degree in degrees)


def test_a_missing_liquidity_feature_is_undetermined_not_safe(thresholds):
    """The silent-zero failure, asserted against."""
    flag = liquidity_degree_flag({"avg_dollar_volume": None}, thresholds)

    assert flag.raised is None
    assert flag.degree is None
    assert flag.unavailable is MissReason.NOT_YET_AVAILABLE
    assert flag.detail["reason"] == FEATURE_UNAVAILABLE


def test_no_feature_vector_at_all_is_reported_distinctly(thresholds):
    """ "No vector" and "vector without this feature" are different facts."""
    flag = liquidity_degree_flag(None, thresholds)

    assert flag.raised is None
    assert flag.detail["reason"] == NO_FEATURE_VECTOR


# --------------------------------------------------------------------------
# Volatility spike
# --------------------------------------------------------------------------


def test_volatility_spike_requires_both_extremity_and_expansion(thresholds):
    """The pairing is the whole point of the flag.

    Extreme-but-stable is a characteristic of the security; expanding-from-
    quiet is the setup ARGUS looks for. Only both together describe
    something having changed.
    """
    both = volatility_spike_flag(
        {"atr_percentile": 0.95, "volatility_compression": 2.0}, thresholds
    )
    always_volatile = volatility_spike_flag(
        {"atr_percentile": 0.95, "volatility_compression": 1.0}, thresholds
    )
    waking_up = volatility_spike_flag(
        {"atr_percentile": 0.40, "volatility_compression": 2.0}, thresholds
    )

    assert both.raised is True
    assert always_volatile.raised is False
    assert waking_up.raised is False


def test_the_spike_flag_names_the_features_it_could_not_read(thresholds):
    flag = volatility_spike_flag(
        {"atr_percentile": 0.95, "volatility_compression": None}, thresholds
    )

    assert flag.raised is None
    assert flag.detail["features"] == ["volatility_compression"]
    assert flag.unavailable is MissReason.NOT_YET_AVAILABLE


def test_every_flag_records_the_thresholds_it_applied(thresholds):
    """A stored verdict must be re-derivable without the code that made it."""
    liquidity = liquidity_degree_flag({"avg_dollar_volume": 100.0}, thresholds)
    spike = volatility_spike_flag(
        {"atr_percentile": 0.1, "volatility_compression": 0.1}, thresholds
    )
    proximity = event_proximity_flag(_view(coverage=EventCoverage.KNOWN, days=3.0), thresholds)

    assert liquidity.detail["comfortable_dollar_volume"] == pytest.approx(
        thresholds.comfortable_dollar_volume.value
    )
    assert spike.detail["atr_percentile_threshold"] == pytest.approx(
        thresholds.volatility_spike_percentile.value
    )
    assert spike.detail["expansion_threshold"] == pytest.approx(
        thresholds.volatility_spike_expansion.value
    )
    assert proximity.detail["imminent_event_days"] == pytest.approx(
        thresholds.imminent_event_days.value
    )


def test_a_flag_that_is_undetermined_always_says_why(thresholds):
    """`raised is None` and `unavailable is None` together would be a
    result that admits nothing and explains nothing."""
    undetermined = [
        liquidity_degree_flag(None, thresholds),
        volatility_spike_flag(None, thresholds),
        event_proximity_flag(
            _view(coverage=EventCoverage.UNAVAILABLE, miss_reason=MissReason.NEVER_INGESTED),
            thresholds,
        ),
    ]
    for flag in undetermined:
        assert flag.raised is None
        assert flag.unavailable is not None, flag.name
        assert flag.degree is None, flag.name


def test_a_determined_flag_never_carries_a_miss_reason(thresholds):
    """The converse. `unavailable` set on a real verdict would let a
    caller filtering on it discard measurements that actually happened."""
    determined = [
        liquidity_degree_flag({"avg_dollar_volume": 1.0}, thresholds),
        volatility_spike_flag({"atr_percentile": 0.5, "volatility_compression": 0.5}, thresholds),
        event_proximity_flag(_view(coverage=EventCoverage.NONE_SCHEDULED), thresholds),
    ]
    for flag in determined:
        assert flag.raised is not None
        assert flag.unavailable is None, flag.name
