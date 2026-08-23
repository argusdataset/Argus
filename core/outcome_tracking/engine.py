"""Turning a concluded setup into a permanent case record.

## What this module is for

ARGUS's central claim — that its pattern has predictive value — can only
ever be tested against the dataset this module builds. A leaked future
price, a mismeasured entry, a silently discarded failure: each produces a
plausible number, and every statistic downstream inherits the error
invisibly, including the eventual answer to whether any of this works.

So the discipline here is the same one Module 07 established for prices
and Module 12 for risk: `as_of` is a plain argument, it bounds every read,
and an adversarial test proves it reaches the loader rather than being
carried decoratively.

## One outcome per setup, written once

`setup_outcomes` is unique on `setup_id` and guarded against DELETE. A
re-run inserts nothing rather than raising: recomputing an outcome under a
*different* snapshot is a legitimate thing to want, but it is a new
snapshot's question, and silently overwriting the row a Module 17 run
already read would make that run unreproducible.

## Failures get the same code path as successes

There is no branch on `OutcomeStatus` anywhere in the assembly. That is
the mechanism behind the guarantee that a `FAILED` case is exactly as
complete as a `SUCCESS` one — not a convention to be remembered, but the
absence of any place to forget it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection

from core.data_validation.corporate_actions import get_corporate_actions_as_of
from core.data_validation.engine import select_latest_as_of
from core.historical_similarity.engine import load_same_asset_history
from core.lifecycle.derivation import history
from core.lifecycle.events import SetupEvent
from core.outcome_tracking.case_record import (
    AWAKENING_METRICS,
    CONSOLIDATION_METRICS,
    CONTEXT_METRICS,
    DECLINE_METRICS,
    CaseRecord,
    SameAssetHistory,
    build_stage_checklist,
    select_metrics,
)
from core.outcome_tracking.classification import ClassificationInputs, classify
from core.outcome_tracking.config import OutcomeConfig
from core.outcome_tracking.excursion import Excursion, measure, outcome_window
from infra.db.schema.intelligence import pending_material_events
from infra.db.schema.setups import setup_outcomes, setups
from infra.db.schema.versioning import feature_vectors


class SetupNotConcluded(ValueError):
    """A setup with no terminal event has no outcome to compute."""


@dataclass(frozen=True, slots=True)
class OutcomeReport:
    """The result of processing a batch of concluded setups."""

    as_of: datetime
    data_snapshot_id: UUID
    cases: list[CaseRecord] = field(default_factory=list)
    written: list[UUID] = field(default_factory=list)
    skipped: dict[UUID, str] = field(default_factory=dict)

    def by_status(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for case in self.cases:
            key = case.classification.status.value
            tally[key] = tally.get(key, 0) + 1
        return tally

    def __len__(self) -> int:
        return len(self.cases)


def compute_case(
    connection: Connection,
    setup_id: UUID,
    *,
    as_of: datetime,
    data_snapshot_id: UUID,
    config: OutcomeConfig | None = None,
    benchmark_security_id: UUID | None = None,
    feature_schema_version_id: UUID | None = None,
) -> CaseRecord:
    """Everything known about one concluded setup, as knowable at `as_of`.

    `as_of` is a plain argument and bounds every read this makes: the
    bars, the corporate actions applied to them, the feature vector, the
    scheduled events, and the security's own transition history.
    """
    config = config or OutcomeConfig()
    thresholds = config.thresholds

    events = history(connection, setup_id, as_of=as_of)
    if not events:
        raise SetupNotConcluded(f"Setup {setup_id} has no events knowable at {as_of}.")

    stages = build_stage_checklist(events)
    if stages.concluded_at is None:
        raise SetupNotConcluded(
            f"Setup {setup_id} has not reached OUTCOME as of {as_of}; there is no "
            "endpoint to measure to."
        )

    row = connection.execute(select(setups).where(setups.c.id == setup_id)).one()
    security_id = row.security_id
    terminal = events[-1]

    excursion, window_events, actions = _measure(
        connection,
        security_id,
        stages=stages,
        as_of=as_of,
        thresholds=thresholds,
        benchmark_security_id=benchmark_security_id,
    )

    # No schema version, no feature block. A setup's `setups` row does not
    # record which feature schema described it — flagged in the module
    # report — so the caller supplies it, and its absence produces an
    # honestly empty metric block rather than a lookup against the wrong
    # version's vectors.
    features = (
        None
        if feature_schema_version_id is None
        else _features_at_detection(
            connection,
            security_id,
            detected_at=stages.detected_at,
            as_of=as_of,
            feature_schema_version_id=feature_schema_version_id,
        )
    )

    classification = classify(
        ClassificationInputs(
            excursion=excursion,
            terminal_event_type=stages.terminal_event_type or terminal.event_type,
            status_before=_status_before(terminal, events),
            activated_at=stages.activated_at,
            corporate_actions_in_window=actions,
            events_in_window=window_events,
            avg_dollar_volume=(features or {}).get("avg_dollar_volume"),
        ),
        thresholds,
    )

    same_asset = load_same_asset_history(connection, security_id, as_of)
    prior = connection.execute(
        select(setup_outcomes.c.id)
        .join(setups, setups.c.id == setup_outcomes.c.setup_id)
        .where(
            and_(
                setups.c.security_id == security_id,
                setups.c.detected_at < stages.detected_at,
            )
        )
    ).all()

    return CaseRecord(
        setup_id=setup_id,
        security_id=security_id,
        stages=stages,
        excursion=excursion,
        classification=classification,
        decline_metrics=select_metrics(features, DECLINE_METRICS),
        consolidation_metrics=select_metrics(features, CONSOLIDATION_METRICS),
        awakening_metrics=select_metrics(features, AWAKENING_METRICS),
        context_metrics=select_metrics(features, CONTEXT_METRICS),
        market_regime_at_outcome=terminal.payload.get("market_state"),
        corporate_actions_in_window=actions,
        events_in_window=window_events,
        same_asset_history=SameAssetHistory(
            cycle_counts=dict(same_asset.cycle_counts),
            backward_transitions=same_asset.backward_transitions,
            prior_cases=len(prior),
        ),
        lineage={
            "target_model_version_id": str(row.target_model_version_id),
            "detection_configuration_id": str(row.detection_configuration_id),
            "universe_version_id": str(row.universe_version_id),
            "data_snapshot_id": str(data_snapshot_id),
            "outcome_configuration": config.version_label(),
        },
    )


def record_outcome(
    connection: Connection, case: CaseRecord, *, data_snapshot_id: UUID
) -> UUID | None:
    """Write one case's outcome row. None if one already exists.

    Every column is populated for every status. A `NO_VALID_OUTCOME` row
    carries real fields explaining why rather than being absent — the
    absence of a row and a setup that could not be measured are different
    facts, and only one of them is knowable from an empty table.
    """
    from sqlalchemy.dialects.postgresql import insert

    excursion = case.excursion
    classification = case.classification
    statement = (
        insert(setup_outcomes)
        .values(
            setup_id=case.setup_id,
            outcome_status=classification.status.value,
            mfe=excursion.mfe,
            mae=excursion.mae,
            time_to_mfe=excursion.time_to_mfe,
            time_to_mae=excursion.time_to_mae,
            outcome_window=excursion.window.duration if excursion.window else None,
            realized_return=excursion.realized_return,
            benchmark_relative_return=excursion.benchmark_relative_return,
            volatility_adjusted_outcome=excursion.volatility_adjusted_outcome,
            market_regime_at_outcome=case.market_regime_at_outcome,
            review_confidence=classification.review_confidence.value,
            false_positive_type=(
                classification.false_positive_type.value
                if classification.false_positive_type
                else None
            ),
            data_snapshot_id=data_snapshot_id,
        )
        .on_conflict_do_nothing(index_elements=["setup_id"])
        .returning(setup_outcomes.c.id)
    )
    return connection.execute(statement).scalar_one_or_none()


def process_concluded_setups(
    connection: Connection,
    *,
    as_of: datetime,
    data_snapshot_id: UUID,
    config: OutcomeConfig | None = None,
    benchmark_security_id: UUID | None = None,
    feature_schema_version_id: UUID | None = None,
) -> OutcomeReport:
    """Compute and record outcomes for every setup concluded by `as_of`.

    Skips setups that already have an outcome row rather than recomputing
    them: the stored row cites a snapshot, and a Module 17 run that read
    it must keep reading the same thing.
    """
    config = config or OutcomeConfig()
    pending = (
        connection.execute(
            select(setups.c.id)
            .outerjoin(setup_outcomes, setup_outcomes.c.setup_id == setups.c.id)
            .where(
                and_(
                    setups.c.concluded_at.is_not(None),
                    setups.c.concluded_at <= as_of,
                    setup_outcomes.c.id.is_(None),
                )
            )
            .order_by(setups.c.concluded_at)
        )
        .scalars()
        .all()
    )

    report = OutcomeReport(as_of=as_of, data_snapshot_id=data_snapshot_id)
    for setup_id in pending:
        try:
            case = compute_case(
                connection,
                setup_id,
                as_of=as_of,
                data_snapshot_id=data_snapshot_id,
                config=config,
                benchmark_security_id=benchmark_security_id,
                feature_schema_version_id=feature_schema_version_id,
            )
        except SetupNotConcluded as error:
            report.skipped[setup_id] = str(error)
            continue
        report.cases.append(case)
        written = record_outcome(connection, case, data_snapshot_id=data_snapshot_id)
        if written is not None:
            report.written.append(written)
    return report


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _measure(
    connection: Connection,
    security_id: UUID,
    *,
    stages,
    as_of: datetime,
    thresholds,
    benchmark_security_id: UUID | None,
) -> tuple[Excursion, tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """The excursion and the context that fell inside its window.

    A setup that never activated has no entry, so there is no window and
    no excursion — reported as such, never as an excursion of zero.
    """
    if stages.activated_at is None or stages.concluded_at is None:
        return (Excursion(unavailable=("no_entry_event",)), (), ())

    window = outcome_window(
        entry_at=stages.activated_at,
        terminal_at=stages.concluded_at,
        thresholds=thresholds,
    )
    excursion = measure(
        connection,
        security_id,
        window=window,
        as_of=as_of,
        thresholds=thresholds,
        benchmark_security_id=benchmark_security_id,
    )
    return (
        excursion,
        _events_in_window(connection, security_id, window=window, as_of=as_of),
        _actions_in_window(connection, security_id, window=window, as_of=as_of),
    )


def _events_in_window(
    connection: Connection, security_id: UUID, *, window, as_of: datetime
) -> tuple[dict[str, Any], ...]:
    """Scheduled binary events that fell inside the outcome window.

    Module 12's `pending_events_as_of` answers "what is coming up from
    here"; this asks "what fell inside a window that has already closed".
    Different questions, and the same PIT rule — the
    `availability_time <= as_of` filter is applied identically, because it
    is the filter that prevents a backtest from knowing about an earnings
    date announced after the fact. A shared helper would be better than
    two callers of one rule; flagged in the module report.
    """
    rows = connection.execute(
        select(pending_material_events).where(
            and_(
                pending_material_events.c.security_id == security_id,
                pending_material_events.c.availability_time <= as_of,
                pending_material_events.c.scheduled_for >= window.entry_at,
                pending_material_events.c.scheduled_for <= window.ends_at,
            )
        )
    ).all()
    # Collapse re-observations of one event, keeping the earliest moment
    # it was knowable — the same rule Module 12 applies, for the same
    # reason: knowledge is not un-learned.
    collapsed: dict[tuple[str, datetime], dict[str, Any]] = {}
    for row in rows:
        key = (row.event_type, row.scheduled_for)
        existing = collapsed.get(key)
        if existing is not None and existing["known_from"] <= row.availability_time:
            continue
        collapsed[key] = {
            "event_type": row.event_type,
            "scheduled_for": row.scheduled_for,
            "is_binary": bool(row.is_binary),
            "known_from": row.availability_time,
        }
    return tuple(sorted(collapsed.values(), key=lambda event: event["scheduled_for"]))


def _actions_in_window(
    connection: Connection, security_id: UUID, *, window, as_of: datetime
) -> tuple[dict[str, Any], ...]:
    """Corporate actions effective inside the window, knowable at `as_of`.

    Straight through Module 07's reader — the whole point of that helper
    is that a caller cannot get the availability filter wrong by
    reimplementing it.
    """
    actions = get_corporate_actions_as_of(connection, security_id, as_of)
    entry, ends = window.entry_at.date(), window.ends_at.date()
    return tuple(
        {
            "action_type": action.action_type.value,
            "effective_date": action.effective_date.isoformat(),
            "availability_time": action.availability_time,
        }
        for action in actions
        if entry <= action.effective_date <= ends
    )


def _features_at_detection(
    connection: Connection,
    security_id: UUID,
    *,
    detected_at: datetime,
    as_of: datetime,
    feature_schema_version_id: UUID,
) -> dict[str, Any] | None:
    """The most recent feature vector knowable when the setup was detected.

    Bounded by `detected_at`, not by `as_of`: the case record should
    describe the base as ARGUS saw it when it opened the setup, not as it
    looked once the outcome was known. Built on Module 07's
    `select_latest_as_of` with `event_time` precedence — the same
    arrangement `get_latest_fundamental_as_of` uses, and for the same
    reason: the key does not pin a single logical record, so the newest
    *event* must win before the newest availability does.

    Module 07 has no "latest feature vector" reader of its own; flagged in
    the module report.
    """
    row = select_latest_as_of(
        connection,
        feature_vectors,
        key={
            "security_id": security_id,
            "feature_schema_version_id": feature_schema_version_id,
        },
        as_of=min(detected_at, as_of),
        precedence=("event_time",),
    )
    if row is None:
        return None
    payload = dict(row.features or {})
    payload.pop("_evidence", None)
    return payload


def _status_before(terminal: SetupEvent, events: list[SetupEvent]):
    """The lifecycle status the setup held before its terminal event.

    Preferring what Module 14 recorded on the payload, falling back to the
    event before it. The payload is authoritative — it was written by the
    module that made the transition — but a terminal event appended
    directly may not carry it.
    """
    from infra.db.enums import SetupLifecycleStatus

    recorded = terminal.payload.get("status_before")
    if recorded is not None:
        return SetupLifecycleStatus(recorded)
    earlier = [event for event in events if event.sequence_number < terminal.sequence_number]
    return earlier[-1].lifecycle_status if earlier else SetupLifecycleStatus.DETECTION
