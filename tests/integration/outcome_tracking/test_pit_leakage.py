"""The adversarial test: an outcome must not know what came after it.

This is the leak that would be invisible without a test built to find it.
Every statistic ARGUS ever reports is computed from outcomes this module
writes, and a leaked future price produces a *plausible* number — a
slightly better MFE, a slightly higher hit rate — rather than an error.
Nothing downstream would notice, including the eventual answer to whether
the pattern works.

Two constructions, because there are two ways for the future to reach
backwards into a price series:

* **A revised bar.** Module 05 files a restatement as a new row with a
  later availability time. The revised bar genuinely exists; only the
  `availability_time <= as_of` filter keeps it out of an earlier query.
* **A late-filed corporate action.** A split backfilled weeks after it
  took effect rescales every bar before it. Applied to a window that
  closed before ARGUS knew about the split, it changes MFE by the split
  ratio.

Both are proven load-bearing by breaking the enforcement and watching
these tests fail — see the module report.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.outcome_tracking.config import OutcomeConfig, publish_outcome_snapshot
from core.outcome_tracking.engine import compute_case
from tests.integration.outcome_tracking.conftest import (
    FIRST_BAR,
    revise_bar,
    write_bars,
    write_split,
)

#: A gentle uptrend: 120 sessions rising about 0.15% a day. Smooth enough
#: that a single revised bar stands out unmistakably.
CLOSES = [100.0 * (1.0015**step) for step in range(120)]

DETECTED = datetime(2024, 1, 16, 21, 0, tzinfo=UTC)
ACTIVATED = datetime(2024, 1, 30, 21, 0, tzinfo=UTC)
TERMINAL = datetime(2024, 3, 26, 21, 0, tzinfo=UTC)

#: Before anything late was filed, and after. Both are after the terminal
#: event, so the setup is concluded either way — the only thing that
#: differs is what ARGUS could know.
BEFORE = datetime(2024, 7, 1, 21, 0, tzinfo=UTC)
AFTER = datetime(2024, 9, 2, 21, 0, tzinfo=UTC)
FILED_AT = datetime(2024, 8, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def case_builder(connection, register, concluded_setup, lineage):
    """One concluded setup over a clean series, ready to be measured twice."""

    def _build(ticker: str):
        security_id = register(ticker)
        write_bars(connection, security_id, CLOSES, start=FIRST_BAR)
        setup_id = concluded_setup(
            security_id,
            detected_at=DETECTED,
            activated_at=ACTIVATED,
            terminal_at=TERMINAL,
        )
        return security_id, setup_id

    return _build


def _measure_at(connection, setup_id, as_of):
    snapshot = publish_outcome_snapshot(connection, OutcomeConfig(), as_of=as_of)
    return compute_case(connection, setup_id, as_of=as_of, data_snapshot_id=snapshot).excursion


# --------------------------------------------------------------------------
# A revised bar
# --------------------------------------------------------------------------


def test_a_bar_revised_after_the_query_date_does_not_change_the_outcome(connection, case_builder):
    """The load-bearing test.

    A bar inside the outcome window is revised to a 40% spike, filed in
    August. A computation made in July must not see it — not because the
    revision is wrong, but because ARGUS did not have it, and an outcome
    that used it would be reporting foreknowledge as skill.
    """
    security_id, setup_id = case_builder("REVISED")
    baseline = _measure_at(connection, setup_id, BEFORE)

    revise_bar(
        connection,
        security_id,
        event_time=datetime(2024, 2, 15, 21, 0, tzinfo=UTC),
        high=140.0,
        available_at=FILED_AT,
    )

    july = _measure_at(connection, setup_id, BEFORE)
    september = _measure_at(connection, setup_id, AFTER)

    assert july.mfe == pytest.approx(baseline.mfe)
    assert september.mfe > july.mfe
    assert september.mfe > 0.30, "the fixture must produce a revision worth noticing"


def test_the_leak_is_visible_when_the_availability_filter_is_bypassed(connection, case_builder):
    """Proof the test above is load-bearing rather than passing on an
    empty table.

    Runs the query a leaking implementation would run — no availability
    bound — against the same rows, and asserts the revised bar *is* there
    to be leaked. If the fixture were wrong, this would fail too.
    """
    from sqlalchemy import select

    from infra.db.schema.canonical import canonical_ohlcv

    security_id, setup_id = case_builder("PROOF")
    revised_at = datetime(2024, 2, 15, 21, 0, tzinfo=UTC)
    revise_bar(connection, security_id, event_time=revised_at, high=140.0, available_at=FILED_AT)

    unbounded = (
        connection.execute(
            select(canonical_ohlcv.c.high_raw).where(
                canonical_ohlcv.c.security_id == security_id,
                canonical_ohlcv.c.event_time == revised_at,
            )
        )
        .scalars()
        .all()
    )

    assert len(unbounded) == 2, "both the original and the revision must exist"
    assert max(float(value) for value in unbounded) == pytest.approx(140.0)
    assert _measure_at(connection, setup_id, BEFORE).mfe < 0.30


# --------------------------------------------------------------------------
# A late-filed corporate action
# --------------------------------------------------------------------------


def test_a_split_filed_after_the_query_date_does_not_rescale_the_window(connection, case_builder):
    """The second leak, and the more damaging one.

    A 2-for-1 split effective inside the window, filed in August. Applied
    to a July computation it halves every bar before the effective date,
    which turns a quiet uptrend into a violent one and changes MFE by the
    split ratio. Unlike a revised high, this distorts the whole series
    rather than one point.
    """
    security_id, setup_id = case_builder("LATESPLIT")
    baseline = _measure_at(connection, setup_id, BEFORE)

    write_split(
        connection,
        security_id,
        effective_date=datetime(2024, 2, 15, tzinfo=UTC),
        available_at=FILED_AT,
    )

    july = _measure_at(connection, setup_id, BEFORE)
    september = _measure_at(connection, setup_id, AFTER)

    assert july.mfe == pytest.approx(baseline.mfe)
    assert july.entry_price == pytest.approx(baseline.entry_price)
    # Once knowable, the split halves everything before its effective
    # date, so the entry price drops and the excursion changes.
    assert september.entry_price == pytest.approx(july.entry_price / 2.0, rel=1e-6)
    assert september.mfe != pytest.approx(july.mfe)


def test_a_split_knowable_all_along_is_applied_at_both_dates(connection, case_builder):
    """The converse, so the test above is about *availability* and not
    about splits being ignored entirely. An action ARGUS knew about must
    be applied — refusing it would be a different bug with the same
    symptom in the first test."""
    security_id, setup_id = case_builder("EARLYSPLIT")
    write_split(
        connection,
        security_id,
        effective_date=datetime(2024, 2, 15, tzinfo=UTC),
        available_at=datetime(2024, 2, 14, tzinfo=UTC),
    )

    july = _measure_at(connection, setup_id, BEFORE)
    september = _measure_at(connection, setup_id, AFTER)

    assert july.entry_price == pytest.approx(september.entry_price)


# --------------------------------------------------------------------------
# The window itself
# --------------------------------------------------------------------------


def test_the_window_stops_at_the_terminal_event(connection, case_builder):
    """Measuring past the terminal event would attribute price action to a
    setup ARGUS had already stopped tracking — the thing that makes a
    backtest flatter than reality."""
    _security_id, setup_id = case_builder("BOUNDED")

    excursion = _measure_at(connection, setup_id, AFTER)

    assert excursion.window.ends_at == TERMINAL
    assert excursion.window.ends_because == "terminal_event"
    assert excursion.window.entry_at == ACTIVATED


def test_the_window_stops_at_the_horizon_when_the_setup_outlives_it(
    connection, register, concluded_setup
):
    """A criterion stated as "within 60 trading days" must not quietly
    become "eventually" for a setup that stayed open longer."""
    security_id = register("LONGRUN")
    write_bars(connection, security_id, CLOSES, start=FIRST_BAR)
    setup_id = concluded_setup(
        security_id,
        detected_at=DETECTED,
        activated_at=ACTIVATED,
        terminal_at=datetime(2024, 6, 20, 21, 0, tzinfo=UTC),
    )

    excursion = _measure_at(connection, setup_id, AFTER)

    assert excursion.window.ends_because == "horizon"
    assert excursion.window.ends_at < datetime(2024, 6, 20, 21, 0, tzinfo=UTC)
    assert excursion.window.ends_at == excursion.window.horizon_ends_at


def test_the_entry_bars_own_excursion_is_not_counted(connection, register, concluded_setup):
    """Entry happens at the activation session's close, so that session's
    own high and low happened around the moment of entry. Counting them
    would credit a setup with favourable excursion it was never
    positioned for.

    The fixture makes the entry bar the highest point of the whole window
    — the series falls from activation onward — so including it would set
    MFE to the entry bar's own intrabar high (+0.5% by construction) while
    excluding it leaves MFE negative. A gently rising series cannot tell
    the two apart, which is how the first version of this test passed a
    deliberate break.
    """
    import pandas as pd

    security_id = register("ENTRYBAR")
    # Find which bar the activation lands on rather than assuming it, then
    # make that bar the peak. Guessing the index is what made the first
    # version of this test unable to tell the two behaviours apart.
    dates = pd.bdate_range(FIRST_BAR, periods=len(CLOSES), tz="UTC")
    activated = ACTIVATED
    peak = max(index for index, day in enumerate(dates) if day.date() <= activated.date())
    falling = CLOSES[: peak + 1] + [
        CLOSES[peak] * (0.99**step) for step in range(1, len(CLOSES) - peak)
    ]
    write_bars(connection, security_id, falling, start=FIRST_BAR)
    setup_id = concluded_setup(
        security_id,
        detected_at=DETECTED,
        activated_at=activated,
        terminal_at=TERMINAL,
    )

    excursion = _measure_at(connection, setup_id, AFTER)

    assert excursion.mfe < 0.0, "the fixture must fall from entry onward"
    # The entry bar's own high sits at +0.5% of its close by construction;
    # counting it would drag MFE up to exactly that.
    assert excursion.mfe < 0.005
