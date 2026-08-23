"""The CASE record: everything known about one setup, assembled.

## It is a projection, not a table

Every part of a CASE record is already stored somewhere — the stage
checklist in `setup_events`, the metrics in `feature_vectors`, the numbers
in `setup_outcomes`, the context in `canonical_corporate_actions` and
`pending_material_events`, the same-asset history in
`market_state_transitions`. This module joins them; it does not copy them
into a new home.

That is a deliberate choice rather than a missing migration.
`setup_outcomes` has no JSONB column, so materializing the record would
have meant either adding one or scattering the parts across typed columns
— and a materialized copy of six tables is a copy that can disagree with
all six. The record is cheap to rebuild and always current. **If Module 16
or 17 needs it materialized** (for an export, or to freeze the assembly
logic alongside the data), that is the point to add a JSONB column or a
database view, and the shape below is what it should hold.

## Failures carry the same fields as successes

Not by convention — by construction. The assembly does not branch on
`OutcomeStatus` anywhere, so a `FAILED` record is built by the same code
path, with the same fields, as a `SUCCESS`. A test asserts the two have
identical field coverage, because a dataset with richer information about
wins than losses teaches Module 17 that wins are more knowable, which is
exactly backwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from core.lifecycle.events import RETREAT, SetupEvent
from core.outcome_tracking.classification import OutcomeClassification
from core.outcome_tracking.excursion import Excursion
from infra.db.enums import SetupLifecycleStatus

#: Module 08 Group B features describing the base. Named here so the CASE
#: record's metric block is a stated list rather than whatever the feature
#: vector happened to contain that day.
CONSOLIDATION_METRICS: tuple[str, ...] = (
    "normalized_range_width",
    "atr_percentile",
    "volatility_compression",
    "volume_contraction",
    "support_test_count",
    "resistance_test_count",
    "failed_breakdown_count",
    "higher_low_development",
)

#: Module 08 Group C features describing the base waking up.
AWAKENING_METRICS: tuple[str, ...] = (
    "volatility_reexpansion",
    "volume_expansion",
    "rvol_increase",
    "range_expansion",
    "resistance_pressure",
    "higher_high_frequency",
    "momentum_improvement",
    "time_in_upper_range",
)

#: Module 08 Group A features describing the decline the setup began from.
DECLINE_METRICS: tuple[str, ...] = (
    "peak_to_trough_decline",
    "decline_duration_bars",
    "decline_speed",
    "downside_momentum_reduction",
)

#: Context features, from Group E.
CONTEXT_METRICS: tuple[str, ...] = (
    "market_regime_trend",
    "market_regime_volatility",
    "market_regime_drawdown",
    "avg_dollar_volume",
    "spread_proxy",
    "rs_vs_market",
)


@dataclass(frozen=True, slots=True)
class StageChecklist:
    """Which lifecycle stages this setup reached, and when.

    Derived from the event history rather than from the outcome, so a
    setup that died at DETECTION says so plainly instead of appearing as
    an activated setup with missing numbers.
    """

    detected_at: datetime
    qualified_at: datetime | None
    activated_at: datetime | None
    concluded_at: datetime | None
    terminal_event_type: str | None
    stages_reached: tuple[str, ...]
    retreat_count: int
    retreats: tuple[datetime, ...]
    events_recorded: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "detected_at": self.detected_at.isoformat(),
            "qualified_at": _iso(self.qualified_at),
            "activated_at": _iso(self.activated_at),
            "concluded_at": _iso(self.concluded_at),
            "terminal_event_type": self.terminal_event_type,
            "stages_reached": list(self.stages_reached),
            "retreat_count": self.retreat_count,
            "retreats": [stamp.isoformat() for stamp in self.retreats],
            "events_recorded": self.events_recorded,
        }


@dataclass(frozen=True, slots=True)
class SameAssetHistory:
    """This security's own record, for context on the case.

    Facts only — Module 10's stored transition counts and Module 11's
    same-asset analogue count. Module 10's `confidence` is deliberately
    absent for the reason its own report gave: it is an unvalidated
    pattern-match score, and a case record that carried it would invite
    Module 17 to treat it as evidence.
    """

    cycle_counts: dict[str, int] = field(default_factory=dict)
    backward_transitions: int = 0
    prior_cases: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_counts": dict(self.cycle_counts),
            "backward_transitions": self.backward_transitions,
            "prior_cases": self.prior_cases,
        }


@dataclass(frozen=True, slots=True)
class CaseRecord:
    """One complete case: how it formed, what happened, and in what context."""

    setup_id: UUID
    security_id: UUID
    stages: StageChecklist
    excursion: Excursion
    classification: OutcomeClassification
    decline_metrics: dict[str, float | None] = field(default_factory=dict)
    consolidation_metrics: dict[str, float | None] = field(default_factory=dict)
    awakening_metrics: dict[str, float | None] = field(default_factory=dict)
    context_metrics: dict[str, float | None] = field(default_factory=dict)
    market_regime_at_outcome: str | None = None
    corporate_actions_in_window: tuple[dict[str, Any], ...] = ()
    events_in_window: tuple[dict[str, Any], ...] = ()
    same_asset_history: SameAssetHistory = field(default_factory=SameAssetHistory)
    #: The lineage the setup itself cites, plus the snapshot this outcome
    #: was computed under.
    lineage: dict[str, str | None] = field(default_factory=dict)
    calibration_status: str = "UNVALIDATED_PLACEHOLDERS"

    def as_dict(self) -> dict[str, Any]:
        """The whole record. Identical field structure for every status."""
        return {
            "setup_id": str(self.setup_id),
            "security_id": str(self.security_id),
            "classification": self.classification.status.value,
            "stages": self.stages.as_dict(),
            "decline_metrics": dict(self.decline_metrics),
            "consolidation_metrics": dict(self.consolidation_metrics),
            "awakening_metrics": dict(self.awakening_metrics),
            "context_metrics": dict(self.context_metrics),
            "outcome": self.excursion.as_dict(),
            "verdict": self.classification.as_dict(),
            "context": {
                "market_regime_at_outcome": self.market_regime_at_outcome,
                "corporate_actions_in_window": [
                    _serialize(action) for action in self.corporate_actions_in_window
                ],
                "events_in_window": [_serialize(event) for event in self.events_in_window],
            },
            "same_asset_history": self.same_asset_history.as_dict(),
            "lineage": dict(self.lineage),
            "calibration_status": self.calibration_status,
            "note": (
                "Assembled on demand from setups, setup_events, feature_vectors, "
                "setup_outcomes and the canonical tables. Not stored as a copy — see "
                "core/outcome_tracking/case_record.py."
            ),
        }


def build_stage_checklist(events: list[SetupEvent]) -> StageChecklist:
    """The lifecycle stages this setup reached, from its own history.

    Retreats are counted and dated: Module 14 records them as events
    inside ACTIVE rather than as demotions, and a setup that retreated
    twice before resolving is a materially different case from one that
    went straight through. A record that dropped them would make those two
    indistinguishable.
    """
    ordered = sorted(events, key=lambda event: event.sequence_number)
    first_at = {}
    for event in ordered:
        first_at.setdefault(event.lifecycle_status, event.occurred_at)

    retreats = tuple(event.occurred_at for event in ordered if event.event_type == RETREAT)
    terminal = ordered[-1] if ordered and ordered[-1].is_terminal else None

    return StageChecklist(
        detected_at=first_at[SetupLifecycleStatus.DETECTION],
        qualified_at=first_at.get(SetupLifecycleStatus.QUALIFICATION),
        activated_at=first_at.get(SetupLifecycleStatus.ACTIVE),
        concluded_at=first_at.get(SetupLifecycleStatus.OUTCOME),
        terminal_event_type=terminal.event_type if terminal else None,
        stages_reached=tuple(status.value for status in first_at),
        retreat_count=len(retreats),
        retreats=retreats,
        events_recorded=len(ordered),
    )


def select_metrics(
    features: dict[str, float | None] | None, names: tuple[str, ...]
) -> dict[str, float | None]:
    """The named metrics, with absent ones present as None.

    Every name appears in the result whether or not it was computed, so a
    case record has the same keys regardless of what the feature vector
    happened to hold. A missing key and a `None` value read very
    differently to whatever consumes this next.
    """
    if features is None:
        return dict.fromkeys(names)
    return {name: features.get(name) for name in names}


def _serialize(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in payload.items()
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
