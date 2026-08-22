"""The three risk flags, each named, each independently determinable.

## A flag has three states, not two

`raised=True`, `raised=False`, and `raised=None`. The third is the one
that matters: "we could not measure this" is not "this is fine", and
collapsing it into `False` is precisely the failure Module 08 built
`FeatureEvidence` to prevent and Module 09 built `INSUFFICIENT_EVIDENCE`
to act on. A risk assessment that silently reports no volatility spike
because the volatility features were never computed is worse than one that
reports nothing at all, because it looks like evidence.

So every flag carries `unavailable: MissReason | None`, set exactly when
`raised is None`, and `degree` is `None` rather than `0.0` when nothing
was measured.

## Degree, not score

`degree` is the continuous reading the flag's threshold was applied to —
average dollar volume expressed as a shortfall, the volatility expansion
multiple, days until the next event. It is one input's measurement, in
that input's own terms, and it is deliberately not comparable across
flags and not summable. Module 13 combines these; this module does not,
and there is a test asserting no aggregate exists.

## What is deliberately absent

No liquidity *gate*. Module 09 already decided pass/fail at a $50,000
floor, and re-deciding it here would put the same judgement in two places
with two thresholds that would drift. This module measures degree above
that floor and says so in the flag's detail.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.data_validation.result import MissReason
from core.risk_context.config import RiskThresholds
from core.risk_context.events import EventCoverage, PendingEventsView

#: Flag names. Referenced by Module 13 and the Terminal, so they are
#: constants rather than strings repeated at call sites.
LIQUIDITY_DEGREE = "liquidity_degree"
VOLATILITY_SPIKE = "volatility_spike"
EVENT_PROXIMITY = "event_proximity"

FLAG_NAMES: tuple[str, ...] = (LIQUIDITY_DEGREE, VOLATILITY_SPIKE, EVENT_PROXIMITY)

#: `detail["reason"]` values distinguishing *why* a reading was absent.
#: `MissReason`'s vocabulary describes as-of queries — it has no term for
#: "the feature vector existed and this feature came out unavailable" —
#: so the closest reason travels in `unavailable` and the precise one
#: travels here. Flagged in the module report rather than resolved by
#: widening an upstream enum.
NO_FEATURE_VECTOR = "no_feature_vector"
FEATURE_UNAVAILABLE = "feature_unavailable"


@dataclass(frozen=True, slots=True)
class RiskFlag:
    """One named risk input: raised, not raised, or not determinable."""

    name: str
    #: None means undetermined. Never coerce it to False.
    raised: bool | None
    #: The reading the threshold was applied to, in its own units. None
    #: when nothing was measured — never 0.0 as a stand-in.
    degree: float | None = None
    #: Measured values and the thresholds applied, so the verdict is
    #: re-derivable from the stored record alone.
    detail: dict[str, Any] = field(default_factory=dict)
    #: Set exactly when `raised is None`.
    unavailable: MissReason | None = None

    @property
    def determined(self) -> bool:
        return self.raised is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "raised": self.raised,
            "degree": self.degree,
            "unavailable": self.unavailable.value if self.unavailable else None,
            "detail": dict(self.detail),
        }


def _undetermined(name: str, reason: str, detail: dict[str, Any]) -> RiskFlag:
    return RiskFlag(
        name=name,
        raised=None,
        degree=None,
        unavailable=MissReason.NOT_YET_AVAILABLE,
        detail={"reason": reason, **detail},
    )


def liquidity_degree_flag(
    features: Mapping[str, float | None] | None,
    thresholds: RiskThresholds,
) -> RiskFlag:
    """How far below comfortable this security's traded value is.

    Module 09 already refused anything under its execution floor, so every
    candidate reaching here is *tradeable*. The remaining question is not
    whether it can be exited but how much the exit will cost, and that is
    continuous: a name at $60,000 a day cleared the same gate as one at
    $5,000,000 and is a materially harder position to leave.

    `degree` runs 0 (at or above comfortable) to 1 (no volume at all), so
    larger means more risk — the same direction as every other flag's
    reading, which matters because Module 13 will be looking at them side
    by side.
    """
    detail: dict[str, Any] = {
        "comfortable_dollar_volume": thresholds.comfortable_dollar_volume.value,
        "note": (
            "Degree only. The pass/fail execution floor is Module 09's "
            "LIQUIDITY gate and is not re-decided here."
        ),
    }
    if features is None:
        return _undetermined(LIQUIDITY_DEGREE, NO_FEATURE_VECTOR, detail)

    value = features.get("avg_dollar_volume")
    if value is None:
        return _undetermined(
            LIQUIDITY_DEGREE,
            FEATURE_UNAVAILABLE,
            {"feature": "avg_dollar_volume", **detail},
        )

    comfortable = thresholds.comfortable_dollar_volume.value
    shortfall = 1.0 - min(1.0, max(0.0, value) / comfortable)
    return RiskFlag(
        name=LIQUIDITY_DEGREE,
        raised=value < comfortable,
        degree=shortfall,
        detail={"avg_dollar_volume": value, **detail},
    )


def volatility_spike_flag(
    features: Mapping[str, float | None] | None,
    thresholds: RiskThresholds,
) -> RiskFlag:
    """Has something changed since these features described a quiet base?

    Two conditions, both required, and the pairing is the point. A high
    ATR percentile alone identifies securities that are *always* volatile
    — a characteristic, not an event. An expansion multiple alone fires on
    any security waking up from an unusually dead stretch, which is the
    setup ARGUS is looking for rather than a risk. Together they describe
    volatility that is both extreme for this name and newly so.

    `volatility_compression` is Module 08's ratio of recent volatility to
    the preceding stretch; below 1 is coiling, above 1 is expansion. Read
    here as an expansion multiple, which is the same number viewed from
    the other end — not a second, redundant computation.
    """
    detail: dict[str, Any] = {
        "atr_percentile_threshold": thresholds.volatility_spike_percentile.value,
        "expansion_threshold": thresholds.volatility_spike_expansion.value,
    }
    if features is None:
        return _undetermined(VOLATILITY_SPIKE, NO_FEATURE_VECTOR, detail)

    percentile = features.get("atr_percentile")
    expansion = features.get("volatility_compression")
    missing = [
        name
        for name, value in (
            ("atr_percentile", percentile),
            ("volatility_compression", expansion),
        )
        if value is None
    ]
    if missing:
        return _undetermined(VOLATILITY_SPIKE, FEATURE_UNAVAILABLE, {"features": missing, **detail})

    assert percentile is not None and expansion is not None
    raised = (
        percentile >= thresholds.volatility_spike_percentile.value
        and expansion >= thresholds.volatility_spike_expansion.value
    )
    return RiskFlag(
        name=VOLATILITY_SPIKE,
        raised=raised,
        # The expansion multiple is the magnitude of the spike; the
        # percentile qualifies whether it is extreme for this name and
        # travels in detail.
        degree=expansion,
        detail={"atr_percentile": percentile, "volatility_expansion": expansion, **detail},
    )


def event_proximity_flag(
    events: PendingEventsView,
    thresholds: RiskThresholds,
) -> RiskFlag:
    """Is a scheduled binary event close enough to decide the next move?

    Not a judgement that earnings are bad. The distinction it preserves is
    between a thesis driven by structure and one that is really
    speculative anticipation of a binary outcome — which look the same on
    a chart and are not the same trade.

    Three outcomes, matching `EventCoverage` exactly: an event is within
    the window, nothing is scheduled and ARGUS could see that, or ARGUS
    could not see. The third never reports as the second.
    """
    detail: dict[str, Any] = {
        "imminent_event_days": thresholds.imminent_event_days.value,
        "horizon_days": events.horizon_days,
        "coverage": events.coverage.value,
    }

    if events.coverage is EventCoverage.UNAVAILABLE:
        return RiskFlag(
            name=EVENT_PROXIMITY,
            raised=None,
            degree=None,
            unavailable=events.miss_reason or MissReason.NEVER_INGESTED,
            detail={
                "reason": (events.miss_reason or MissReason.NEVER_INGESTED).value,
                "note": (
                    "No earnings-calendar coverage was knowable at this as_of. "
                    "This is an absence of information, not an absence of events."
                ),
                **detail,
            },
        )

    if events.coverage is EventCoverage.NONE_SCHEDULED:
        return RiskFlag(
            name=EVENT_PROXIMITY,
            raised=False,
            # No event means no proximity to measure. A large sentinel
            # would be a lie in the same shape as a zero.
            degree=None,
            detail={
                "note": "Calendar coverage was available; no event falls within the horizon.",
                "coverage_observed_at": (
                    events.coverage_observed_at.isoformat() if events.coverage_observed_at else None
                ),
                **detail,
            },
        )

    days = events.days_until_next
    assert days is not None
    event = events.next_event
    assert event is not None
    return RiskFlag(
        name=EVENT_PROXIMITY,
        raised=days <= thresholds.imminent_event_days.value,
        degree=days,
        detail={
            "days_until_next": days,
            "event_type": event.event_type,
            "is_binary": event.is_binary,
            "scheduled_for": event.scheduled_for.isoformat(),
            "known_from": event.known_from.isoformat(),
            "events_in_horizon": len(events.events),
            **detail,
        },
    )
