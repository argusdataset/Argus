"""Builders for classification inputs. Not a test module.

The classifier is pure, so every branch — five outcome statuses, seven
false-positive types, three review confidences — can be reached from a
constructed fixture with no database in the way. That matters more here
than usual: a taxonomy with seven outcomes needs seven cases, and each one
should fail for exactly one reason.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.outcome_tracking.classification import ClassificationInputs
from core.outcome_tracking.excursion import TERMINAL_EVENT, Excursion, OutcomeWindow
from infra.db.enums import SetupLifecycleStatus

ENTRY = datetime(2024, 1, 8, 21, 0, tzinfo=UTC)
TERMINAL = datetime(2024, 3, 8, 21, 0, tzinfo=UTC)

ENDPOINT_REACHED = "endpoint_reached"
EXPIRED_ACTIVE = "expired_active"
EXPIRED_UNQUALIFIED = "expired_unqualified"
INVALIDATED_LOST = "invalidated_lost_eligibility"
INVALIDATED_INELIGIBLE = "invalidated_ineligible"


def window(*, entry: datetime = ENTRY, ends: datetime = TERMINAL) -> OutcomeWindow:
    return OutcomeWindow(
        entry_at=entry,
        ends_at=ends,
        ends_because=TERMINAL_EVENT,
        horizon_ends_at=ends + timedelta(days=30),
        terminal_at=ends,
    )


#: Entry price and entry ATR for the constructed excursions.
#:
#: The ATR is `ENTRY_PRICE / 15`, which is not arbitrary: it makes
#: `atr_fraction` exactly 1/15, and at that fraction the two ATR-multiple
#: CASE floors land exactly on the flat percentages they replaced —
#: `0.45 / 15 = 0.03` and `-2.25 / 15 = -0.15`. So every classification
#: test below goes on testing the same boundaries it always tested, which
#: is the point: re-expressing those floors in ATR moved nothing, and a
#: fixture that had to be re-tuned would have been evidence it did.
ENTRY_PRICE = 100.0
ENTRY_ATR = ENTRY_PRICE / 15.0


def excursion(
    *,
    mfe: float | None = 0.12,
    mae: float | None = -0.03,
    realized: float | None = 0.10,
    target_day: int | None = 20,
    stop_day: int | None = None,
    mfe_day: int | None = 20,
    atr_at_entry: float | None = ENTRY_ATR,
    unavailable: tuple[str, ...] = (),
    measured: bool = True,
) -> Excursion:
    """One measured excursion. `measured=False` gives an unmeasurable one."""
    if not measured:
        return Excursion(window=window(), unavailable=unavailable or ("no_bars_in_window",))
    return Excursion(
        window=window(),
        entry_price=ENTRY_PRICE,
        exit_price=ENTRY_PRICE * (1.0 + (realized or 0.0)),
        atr_at_entry=atr_at_entry,
        mfe=mfe,
        mae=mae,
        time_to_mfe=None if mfe_day is None else timedelta(days=mfe_day),
        time_to_mae=timedelta(days=10),
        realized_return=realized,
        benchmark_relative_return=None if realized is None else realized - 0.01,
        volatility_adjusted_outcome=None if realized is None else realized * 2.0,
        target_hit_at=None if target_day is None else ENTRY + timedelta(days=target_day),
        stop_hit_at=None if stop_day is None else ENTRY + timedelta(days=stop_day),
        bars_observed=42,
        unavailable=unavailable,
    )


def inputs(
    *,
    terminal_event_type: str = ENDPOINT_REACHED,
    status_before: SetupLifecycleStatus = SetupLifecycleStatus.ACTIVE,
    activated: bool = True,
    avg_dollar_volume: float | None = 2_000_000.0,
    actions: tuple[dict[str, Any], ...] = (),
    events: tuple[dict[str, Any], ...] = (),
    **excursion_kwargs,
) -> ClassificationInputs:
    return ClassificationInputs(
        excursion=excursion(**excursion_kwargs),
        terminal_event_type=terminal_event_type,
        status_before=status_before,
        activated_at=ENTRY if activated else None,
        corporate_actions_in_window=actions,
        events_in_window=events,
        avg_dollar_volume=avg_dollar_volume,
    )


def split_action(*, day: int = 15) -> dict[str, Any]:
    return {
        "action_type": "SPLIT",
        "effective_date": (ENTRY + timedelta(days=day)).date().isoformat(),
        "availability_time": ENTRY + timedelta(days=day),
    }


def earnings_event(*, day: int = 20) -> dict[str, Any]:
    return {
        "event_type": "EARNINGS",
        "scheduled_for": ENTRY + timedelta(days=day),
        "is_binary": True,
        "known_from": ENTRY - timedelta(days=30),
    }
