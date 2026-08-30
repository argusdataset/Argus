"""The criterion scales with each security's own volatility, not a flat
percentage.

This is the direct proof of the fix: two securities that make the
*identical* post-entry price move resolve differently depending only on
how volatile they were *before* entry — because their ATR at entry
differs, and the target/stop are measured in ATR multiples rather than
in flat percentage points.

Pre-entry bars affect nothing about `mfe`, `mae` or `realized_return` —
those are measured only from bars after entry (`excursion.py`'s own
rule) — so the pre-entry history in these fixtures isolates ATR as the
only thing that differs between the two cases.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from core.outcome_tracking.config import OutcomeConfig
from core.outcome_tracking.excursion import NO_ATR, measure, outcome_window
from data.canonical_model.pit import session_close
from tests.integration.outcome_tracking.conftest import write_bars

#: 25 quiet pre-entry sessions, essentially flat, ending exactly at 100 so
#: the entry price matches the volatile case below.
QUIET_PRE_ENTRY = [100.0] * 24 + [100.0]
#: 25 pre-entry sessions alternating +/-8%, ending at 100 too — same
#: entry price, wildly different measured volatility.
VOLATILE_PRE_ENTRY = [100.0 * (1.08 if i % 2 else 1.0) for i in range(24)] + [100.0]

#: Identical for both cases: a smooth +3% rise over ten sessions, then
#: flat for the rest of the window. Well inside what the quiet case's
#: threshold reaches and well short of what the volatile case's does.
POST_ENTRY = [100.0 * (1.003**min(step, 10)) for step in range(1, 40)]

AS_OF = datetime(2024, 6, 1, tzinfo=UTC)


def _dates(start: datetime, count: int) -> list[datetime]:
    return [session_close(day.date()) for day in pd.bdate_range(start, periods=count, tz="UTC")]


@pytest.fixture
def measured(connection, register):
    """Build one security from pre-entry + post-entry closes and measure it."""

    def _build(ticker: str, pre_entry: list[float], *, start: datetime):
        security_id = register(ticker)
        closes = pre_entry + POST_ENTRY
        write_bars(connection, security_id, closes, start=start)

        entry_at = _dates(start, len(pre_entry))[-1]
        terminal_at = _dates(start, len(closes))[-1]
        window = outcome_window(
            entry_at=entry_at,
            terminal_at=terminal_at,
            thresholds=OutcomeConfig().thresholds,
        )
        return measure(
            connection,
            security_id,
            window=window,
            as_of=AS_OF,
            thresholds=OutcomeConfig().thresholds,
        )

    return _build


def test_atr_at_entry_reflects_pre_entry_volatility_not_the_move_that_follows(measured):
    """The measurement this whole fix depends on: ATR differs even though
    the post-entry path is byte-identical."""
    quiet = measured("QUIETATR", QUIET_PRE_ENTRY, start=datetime(2024, 1, 2, tzinfo=UTC))
    volatile = measured("VOLATR", VOLATILE_PRE_ENTRY, start=datetime(2024, 1, 2, tzinfo=UTC))

    assert quiet.atr_at_entry is not None
    assert volatile.atr_at_entry is not None
    assert volatile.atr_at_entry > quiet.atr_at_entry * 3


def test_identical_price_action_resolves_differently_by_volatility(measured):
    """The bug this fixes, made concrete.

    Under a flat percentage, these two would have used the identical
    +10%/-5% threshold regardless of how they got to their entry price.
    Under the ATR-relative criterion, the same +3% move that clears the
    quiet security's tighter target does not touch the volatile
    security's much wider one — because the volatile security's own
    recent range says a 3% wiggle is unremarkable for it.
    """
    quiet = measured("QCLASS", QUIET_PRE_ENTRY, start=datetime(2024, 1, 2, tzinfo=UTC))
    volatile = measured("VCLASS", VOLATILE_PRE_ENTRY, start=datetime(2024, 1, 2, tzinfo=UTC))

    assert quiet.target_threshold is not None
    assert volatile.target_threshold is not None
    assert quiet.target_threshold < 0.03  # comfortably below the +3% move
    assert volatile.target_threshold > 0.03  # comfortably above it

    assert quiet.target_hit_at is not None
    assert volatile.target_hit_at is None


def test_the_effective_thresholds_are_the_multiple_times_the_atr_fraction(measured):
    """Recorded, not merely implied — a reviewer reading one row should
    not have to recompute what threshold actually applied."""
    thresholds = OutcomeConfig().thresholds
    record = measured("EFFTHRESH", QUIET_PRE_ENTRY, start=datetime(2024, 1, 2, tzinfo=UTC))

    atr_fraction = record.atr_at_entry / record.entry_price
    assert record.target_threshold == pytest.approx(
        thresholds.target_atr_multiple.value * atr_fraction
    )
    assert record.stop_threshold == pytest.approx(
        thresholds.stop_atr_multiple.value * atr_fraction
    )
    assert record.stop_threshold < 0 < record.target_threshold


def test_too_little_pre_entry_history_reports_no_atr_rather_than_a_flat_fallback(
    connection, register
):
    """A security too newly listed to measure a 20-session ATR gets an
    honest absence, never a silent flat-percentage substitute."""
    start = datetime(2024, 1, 2, tzinfo=UTC)
    security_id = register("TOOFRESH")
    # Three sessions of history before entry -- far short of the
    # 20-session ATR window -- then the same post-entry path.
    closes = [100.0, 100.5, 100.0] + POST_ENTRY
    write_bars(connection, security_id, closes, start=start)

    entry_at = _dates(start, 3)[-1]
    terminal_at = _dates(start, len(closes))[-1]
    thresholds = OutcomeConfig().thresholds
    window = outcome_window(entry_at=entry_at, terminal_at=terminal_at, thresholds=thresholds)

    record = measure(connection, security_id, window=window, as_of=AS_OF, thresholds=thresholds)

    assert record.atr_at_entry is None
    assert record.target_threshold is None
    assert record.stop_threshold is None
    assert record.target_hit_at is None
    assert record.stop_hit_at is None
    assert NO_ATR in record.unavailable
    # mfe/mae are unaffected -- they do not depend on ATR at all.
    assert record.mfe is not None
    assert record.mae is not None
