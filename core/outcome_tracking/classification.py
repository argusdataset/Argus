"""Deciding what happened, and — when it went wrong — why.

## The status decision is criterion-first, terminal-event-second

Module 14's terminal event types map onto `OutcomeStatus` almost
directly, and the Module 15 brief gives that mapping. This module applies
it as the **fallback** rather than as the rule, and the difference is
deliberate:

```
never activated                     -> NO_VALID_OUTCOME
criterion resolved in the window    -> SUCCESS or FAILED
terminal event was an invalidation  -> INVALIDATED
otherwise                           -> EXPIRED
```

In every ordinary case this produces exactly the brief's mapping.
`expired_unqualified` setups never activated, so they land on
`NO_VALID_OUTCOME`; `expired_active` setups activated and, by definition,
ran out of window without resolving, so they land on `EXPIRED`;
`endpoint_reached` setups get the real classification work the brief asks
for.

Where it diverges is the case the literal mapping gets wrong. A setup can
reach `+10%` on day thirty and still be closed by Module 14's expiry or by
an administrative invalidation, because Module 14 watches market states
and eligibility, not the price criterion. Labelling that `EXPIRED` would
put a resolved success into the dataset as an unresolved non-event, and
Module 17's hit rate would be quietly too low. The criterion is the
dataset's ground truth; the lifecycle event says how tracking ended, which
is a different question.

## Both thresholds on one bar resolve as FAILED

A single bar whose high cleared the target and whose low broke the stop is
genuinely ambiguous — daily bars do not record the order. The adverse case
is assumed, which is the conservative convention: it can only understate
the pattern's performance, and a dataset that flatters the thing it exists
to test is worthless.

## The false-positive taxonomy is heuristic, and says so

A–G are assigned from measurable proxies, not from understanding. The
distortion types (G, F, E) are checked first because each *explains away*
the structural ones: a base that "failed" through a split artefact did not
fail as a pattern, and recording it as a type-D breakdown would teach
Module 17 a lesson about pattern structure from an accounting event.

Every classification carries `false_positive_confidence`, and none of the
values is `certain`. The limitations are in the module README and in this
module's report; the honest summary is that E and G are near-certain
*coincidences* rather than proven *causes*, and that A/B/C/D are
threshold-separated regions of one continuous space.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from core.outcome_tracking.config import OutcomeThresholds
from core.outcome_tracking.excursion import Excursion
from infra.db.enums import (
    FalsePositiveType,
    OutcomeStatus,
    ReviewConfidence,
    SetupLifecycleStatus,
)

#: Terminal event types that mean the setup was stopped for a reason
#: outside price. Named rather than inferred from the string, so a new
#: invalidation kind in Module 14 has to be classified here deliberately.
INVALIDATION_EVENTS: frozenset[str] = frozenset(
    {"invalidated_lost_eligibility", "invalidated_ineligible"}
)

#: How much to trust a false-positive label. `certain` is deliberately
#: absent — none of these heuristics establishes cause.
COINCIDENT = "coincident"
INFERRED = "inferred"
WEAK = "weak"


@dataclass(frozen=True, slots=True)
class ClassificationInputs:
    """Everything the decision reads. No I/O — the engine gathers it.

    Keeping this pure is what lets every branch be tested from a
    constructed fixture, which matters more here than usual: the
    false-positive taxonomy has seven outcomes and each needs its own
    case.
    """

    excursion: Excursion
    terminal_event_type: str
    #: The lifecycle status the setup held before its terminal event. A
    #: setup that ended from DETECTION never activated.
    status_before: SetupLifecycleStatus
    activated_at: datetime | None = None
    #: Corporate actions effective inside the outcome window.
    corporate_actions_in_window: tuple[dict[str, Any], ...] = ()
    #: Scheduled binary events falling inside the outcome window.
    events_in_window: tuple[dict[str, Any], ...] = ()
    #: Average dollar volume from the setup's feature vector at detection.
    avg_dollar_volume: float | None = None

    @property
    def activated(self) -> bool:
        return self.activated_at is not None


@dataclass(frozen=True, slots=True)
class OutcomeClassification:
    """What happened, why, and how much to trust the label."""

    status: OutcomeStatus
    reason: str
    false_positive_type: FalsePositiveType | None = None
    false_positive_reason: str = ""
    false_positive_confidence: str | None = None
    review_confidence: ReviewConfidence = ReviewConfidence.LOW
    review_reason: str = ""
    signals: dict[str, Any] = field(default_factory=dict)

    @property
    def is_false_positive(self) -> bool:
        return self.false_positive_type is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "false_positive_type": (
                self.false_positive_type.value if self.false_positive_type else None
            ),
            "false_positive_reason": self.false_positive_reason,
            "false_positive_confidence": self.false_positive_confidence,
            "review_confidence": self.review_confidence.value,
            "review_reason": self.review_reason,
            "signals": dict(self.signals),
        }


def classify(inputs: ClassificationInputs, thresholds: OutcomeThresholds) -> OutcomeClassification:
    """The whole decision: status, false-positive type, review confidence."""
    status, reason = _status(inputs)
    false_positive = _false_positive(inputs, status, thresholds)
    kind, fp_reason, confidence = false_positive
    review, review_reason = _review_confidence(inputs, status, kind)

    return OutcomeClassification(
        status=status,
        reason=reason,
        false_positive_type=kind,
        false_positive_reason=fp_reason,
        false_positive_confidence=confidence,
        review_confidence=review,
        review_reason=review_reason,
        signals={
            "terminal_event_type": inputs.terminal_event_type,
            "status_before": inputs.status_before.value,
            "activated": inputs.activated,
            "criterion_resolved": inputs.excursion.criterion_resolved,
            "target_hit_at": _iso(inputs.excursion.target_hit_at),
            "stop_hit_at": _iso(inputs.excursion.stop_hit_at),
            "corporate_actions_in_window": len(inputs.corporate_actions_in_window),
            "events_in_window": len(inputs.events_in_window),
            "avg_dollar_volume": inputs.avg_dollar_volume,
            "excursion_unavailable": list(inputs.excursion.unavailable),
        },
    )


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


def _status(inputs: ClassificationInputs) -> tuple[OutcomeStatus, str]:
    if not inputs.activated:
        return (
            OutcomeStatus.NO_VALID_OUTCOME,
            "The setup never activated, so it has no entry point and no return to "
            f"measure. It ended from {inputs.status_before.value}.",
        )

    excursion = inputs.excursion
    if not excursion.measured:
        return (
            OutcomeStatus.NO_VALID_OUTCOME,
            "The setup activated but its excursion could not be measured: "
            + ", ".join(excursion.unavailable),
        )

    target, stop = excursion.target_hit_at, excursion.stop_hit_at
    if stop is not None and (target is None or stop <= target):
        # `stop <= target` covers the same-bar case, deliberately. See the
        # module docstring: daily bars do not record intrabar order, and
        # assuming the adverse move can only understate performance.
        same_bar = target is not None and stop == target
        return (
            OutcomeStatus.FAILED,
            "The stop was reached "
            + (
                "on the same bar as the target; the adverse case is assumed."
                if same_bar
                else f"first, at {stop.isoformat()}."
            ),
        )
    if target is not None:
        return (OutcomeStatus.SUCCESS, f"The target was reached first, at {target.isoformat()}.")

    if inputs.terminal_event_type in INVALIDATION_EVENTS:
        return (
            OutcomeStatus.INVALIDATED,
            "Tracking was stopped for a reason outside price "
            f"({inputs.terminal_event_type}) before the criterion resolved.",
        )

    return (
        OutcomeStatus.EXPIRED,
        "The window closed with neither the target nor the stop reached.",
    )


# --------------------------------------------------------------------------
# The A-G taxonomy
# --------------------------------------------------------------------------


def _false_positive(
    inputs: ClassificationInputs,
    status: OutcomeStatus,
    thresholds: OutcomeThresholds,
) -> tuple[FalsePositiveType | None, str, str | None]:
    """Why this setup was an informative negative, if it was.

    A SUCCESS has no false-positive type. Everything else does — including
    EXPIRED and INVALIDATED, because "nothing happened" and "we stopped
    watching" are informative negatives too, and a dataset that only
    classifies the FAILED ones would leave Module 17 blind to the most
    common way this pattern disappoints.
    """
    if status is OutcomeStatus.SUCCESS:
        return (None, "", None)

    if not inputs.activated:
        return (
            FalsePositiveType.A_NO_PATTERN,
            "The structure never advanced far enough to activate, so no pattern "
            "developed to be right or wrong about.",
            INFERRED,
        )

    # -- Distortions first: each explains away the structural verdicts ----
    if inputs.corporate_actions_in_window:
        return (
            FalsePositiveType.G_CORPORATE_ACTION_DISTORTION,
            f"{len(inputs.corporate_actions_in_window)} corporate action(s) took effect "
            "inside the outcome window. The price series is adjusted for splits, but a "
            "coincident action is enough to make the excursion untrustworthy.",
            COINCIDENT,
        )

    volume = inputs.avg_dollar_volume
    if volume is not None and volume < thresholds.illiquid_dollar_volume.value:
        return (
            FalsePositiveType.F_ILLIQUID_DISTORTION,
            f"Average dollar volume of {volume:,.0f} at detection is below the "
            f"{thresholds.illiquid_dollar_volume.value:,.0f} floor: the excursion may be "
            "a print artefact rather than a tradeable move.",
            INFERRED,
        )

    coincident = _coincident_event(inputs, thresholds)
    if coincident is not None:
        return (
            FalsePositiveType.E_CATALYST_DRIVEN,
            coincident,
            COINCIDENT,
        )

    # -- Structural verdicts ------------------------------------------------
    #
    # B, C and D are separated by how far the security moved *relative to
    # its own volatility*, for the same reason the criterion itself is:
    # a flat percentage grades "did this move at all" on a scale that
    # means something different for every name. Both floors arrive as ATR
    # multiples and are resolved against this setup's own entry ATR.
    excursion = inputs.excursion
    atr_fraction = excursion.atr_fraction
    if atr_fraction is None:
        # No volatility scale, so "meaningful move" has no meaning here.
        # Reported as an unclassified negative rather than defaulted into
        # type B, which would assert the structure went nowhere on the
        # strength of a measurement that does not exist. `review_confidence`
        # is already LOW whenever the excursion carries an unavailable
        # reason, so the row is flagged for a human without this inventing
        # a verdict.
        return (
            None,
            "The false-positive type could not be assigned: entry ATR was not "
            "measurable, so there is no scale on which to judge whether this "
            "security's excursion counts as movement. " + ", ".join(excursion.unavailable),
            WEAK,
        )

    expansion_floor = thresholds.expansion_atr_multiple.value * atr_fraction
    breakdown_floor = thresholds.breakdown_atr_multiple.value * atr_fraction

    realized = excursion.realized_return
    if realized is not None and realized <= breakdown_floor:
        return (
            FalsePositiveType.D_BREAKDOWN,
            f"Realized return of {realized:.1%} is past the "
            f"{breakdown_floor:.1%} breakdown floor "
            f"({thresholds.breakdown_atr_multiple.value:g}x this security's entry ATR): "
            "the base gave way rather than merely failing to expand.",
            INFERRED,
        )

    mfe = excursion.mfe
    if mfe is not None and mfe >= expansion_floor:
        return (
            FalsePositiveType.C_FALSE_BREAKOUT,
            f"The setup advanced {mfe:.1%} before failing — it broke out and did not "
            "hold, which is a different lesson from never having moved.",
            INFERRED,
        )

    return (
        FalsePositiveType.B_PATTERN_NO_EXPANSION,
        f"The structure formed but went nowhere: peak favourable excursion of "
        f"{mfe:.1%} is under the {expansion_floor:.1%} floor "
        f"({thresholds.expansion_atr_multiple.value:g}x this security's entry ATR)."
        if mfe is not None
        else "The structure formed but no expansion was measurable.",
        WEAK,
    )


def _coincident_event(inputs: ClassificationInputs, thresholds: OutcomeThresholds) -> str | None:
    """A scheduled binary event sitting on top of the favourable excursion.

    Proximity to the *excursion*, not merely to the window: earnings
    somewhere in a sixty-day window is unremarkable, earnings on the day
    the move happened is the thing type E is about.
    """
    excursion = inputs.excursion
    if not inputs.events_in_window or excursion.time_to_mfe is None:
        return None

    peak_at = excursion.window.entry_at + excursion.time_to_mfe
    tolerance = timedelta(days=thresholds.catalyst_window_days.value)
    for event in inputs.events_in_window:
        scheduled = event.get("scheduled_for")
        if scheduled is None:
            continue
        if abs(scheduled - peak_at) <= tolerance:
            return (
                f"A scheduled {event.get('event_type', 'binary')} event on "
                f"{scheduled.date().isoformat()} sits within "
                f"{thresholds.catalyst_window_days.value:.0f} days of the peak excursion: "
                "the move may be the event rather than the pattern."
            )
    return None


# --------------------------------------------------------------------------
# Review confidence
# --------------------------------------------------------------------------


def _review_confidence(
    inputs: ClassificationInputs,
    status: OutcomeStatus,
    false_positive: FalsePositiveType | None,
) -> tuple[ReviewConfidence, str]:
    """How much to trust the label, pending human review.

    This describes confidence in the **classification**, not in the setup.
    It is an automated placeholder: during dataset construction a reviewer
    overwrites it, and `setup_outcomes` permits UPDATE for exactly that
    (Module 03's deliberate asymmetry). The policy is stated so a reviewer
    knows what an unreviewed value means rather than having to guess.
    """
    excursion = inputs.excursion
    if not excursion.measured and inputs.activated:
        return (
            ReviewConfidence.LOW,
            "The setup activated but no excursion could be measured, so the label "
            "rests on the lifecycle event alone.",
        )
    if excursion.unavailable:
        return (
            ReviewConfidence.LOW,
            "Some inputs were unavailable: " + ", ".join(excursion.unavailable),
        )
    if false_positive in _DISTORTION_TYPES:
        return (
            ReviewConfidence.MEDIUM,
            "A distortion was flagged, and distortion heuristics establish "
            "coincidence rather than cause.",
        )
    if status in (OutcomeStatus.SUCCESS, OutcomeStatus.FAILED):
        return (
            ReviewConfidence.HIGH,
            "The predefined criterion resolved on complete data.",
        )
    return (
        ReviewConfidence.MEDIUM,
        f"The criterion did not resolve; the label ({status.value}) comes from how "
        "tracking ended rather than from price.",
    )


_DISTORTION_TYPES = frozenset(
    {
        FalsePositiveType.E_CATALYST_DRIVEN,
        FalsePositiveType.F_ILLIQUID_DISTORTION,
        FalsePositiveType.G_CORPORATE_ACTION_DISTORTION,
    }
)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
