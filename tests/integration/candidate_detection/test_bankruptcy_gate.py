"""The near-bankrupt security that looks exactly like a base.

The scenario the gate exists for, built end to end: a company whose price
action is a textbook consolidation — falling from a peak, then flat and
quiet for a year — while its fundamentals show a business dying. Module 08
computes real features from the real bars; Module 09 must let it into the
candidate pool (the pattern *is* there) and then exclude it on the
bankruptcy gate rather than scoring it.

That sequence is the whole design. Excluding it at detection would look
tidier and would destroy the distinction: "we rejected this for bankruptcy
risk" is a fact you can count and audit, while "it never entered the pool"
is invisible.

Its healthy twin has identical prices and solvent fundamentals, so any
difference in verdict is attributable to the fundamentals alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection

from core.candidate_detection.config import EligibilityParameters
from core.candidate_detection.eligibility.bankruptcy import (
    evaluate_bankruptcy_gate,
    load_distress_signals,
)
from core.feature_engine.engine import compute_features_batch
from data.canonical_model.records import CanonicalStatementType
from tests.integration.candidate_detection.conftest import decline_then_base, insert_bars

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)

#: Filed well before `as_of`, so everything here is genuinely knowable.
PERIOD_END = datetime(2024, 3, 31, tzinfo=UTC)
FILED = datetime(2024, 5, 10, tzinfo=UTC)
PRIOR_PERIOD_END = datetime(2023, 3, 31, tzinfo=UTC)
PRIOR_FILED = datetime(2023, 5, 10, tzinfo=UTC)

#: 800 business days ending just before `as_of`, so the 252-bar
#: structural window is genuinely full. A series ending months early
#: would leave the long-window features None and make the comparison
#: between the two companies vacuous.
SEASONED_START = datetime(2021, 5, 10)


#: Down hard from a peak, then flat and quiet. A base, or a death spiral —
#: the two are indistinguishable from price alone, which is exactly why the
#: gate reads fundamentals instead.
def _consolidation_prices(bars: int) -> list[float]:
    return decline_then_base(bars, high=50.0, low=6.0)


@pytest.fixture
def dying_company(connection: Connection, register, add_statement) -> UUID:
    """Negative equity, two quarters of runway, and heavy dilution.

    Three signals — comfortably past the two required — because a fixture
    sitting exactly on the boundary would make the test about the
    threshold rather than about the gate working.
    """
    security_id = register("DYING")
    insert_bars(connection, security_id, start=SEASONED_START, closes=_consolidation_prices(800))

    add_statement(
        security_id,
        CanonicalStatementType.BALANCE_SHEET,
        period_end=PERIOD_END,
        available=FILED,
        data={
            "cashAndCashEquivalents": 3_000_000,
            "totalAssets": 20_000_000,
            "totalDebt": 19_500_000,
            "totalStockholdersEquity": -8_000_000,
        },
    )
    add_statement(
        security_id,
        CanonicalStatementType.CASH_FLOW,
        period_end=PERIOD_END,
        available=FILED,
        data={"operatingCashFlow": -2_000_000},
    )
    add_statement(
        security_id,
        CanonicalStatementType.INCOME_STATEMENT,
        period_end=PERIOD_END,
        available=FILED,
        data={"revenue": 1_000_000, "weightedAverageShsOut": 400_000_000},
    )
    add_statement(
        security_id,
        CanonicalStatementType.INCOME_STATEMENT,
        period_end=PRIOR_PERIOD_END,
        available=PRIOR_FILED,
        data={"revenue": 900_000, "weightedAverageShsOut": 100_000_000},
    )
    return security_id


@pytest.fixture
def healthy_twin(connection: Connection, register, add_statement) -> UUID:
    """Identical prices, solvent balance sheet. The control."""
    security_id = register("HEALTHY")
    insert_bars(connection, security_id, start=SEASONED_START, closes=_consolidation_prices(800))

    add_statement(
        security_id,
        CanonicalStatementType.BALANCE_SHEET,
        period_end=PERIOD_END,
        available=FILED,
        data={
            "cashAndCashEquivalents": 120_000_000,
            "totalAssets": 300_000_000,
            "totalDebt": 40_000_000,
            "totalStockholdersEquity": 210_000_000,
        },
    )
    add_statement(
        security_id,
        CanonicalStatementType.CASH_FLOW,
        period_end=PERIOD_END,
        available=FILED,
        data={"operatingCashFlow": -4_000_000},
    )
    add_statement(
        security_id,
        CanonicalStatementType.INCOME_STATEMENT,
        period_end=PERIOD_END,
        available=FILED,
        data={"revenue": 80_000_000, "weightedAverageShsOut": 102_000_000},
    )
    add_statement(
        security_id,
        CanonicalStatementType.INCOME_STATEMENT,
        period_end=PRIOR_PERIOD_END,
        available=PRIOR_FILED,
        data={"revenue": 75_000_000, "weightedAverageShsOut": 100_000_000},
    )
    return security_id


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_the_dying_company_is_excluded(connection: Connection, dying_company: UUID):
    """Three signals fire, and the gate excludes."""
    signals = load_distress_signals(connection, [dying_company], AS_OF)[dying_company]
    result = evaluate_bankruptcy_gate(signals, EligibilityParameters())

    assert not result.passed
    assert signals.signal_count >= 2
    assert "negative_shareholders_equity" in signals.fired


def test_each_signal_fires_for_the_reason_it_claims(connection: Connection, dying_company: UUID):
    """Named signals, checkable individually — not an opaque distress score.

    An explainability requirement, not a nicety: a rejection ARGUS cannot
    decompose into measured quantities is a rejection it should not make.
    """
    signals = load_distress_signals(connection, [dying_company], AS_OF)[dying_company]

    assert set(signals.fired) == {
        "negative_shareholders_equity",
        "short_cash_runway",
        "extreme_leverage",
        "heavy_dilution",
    }
    assert signals.measured["shareholders_equity"] == pytest.approx(-8_000_000.0)
    assert signals.measured["runway_quarters"] == pytest.approx(1.5)
    assert signals.measured["debt_to_assets"] == pytest.approx(0.975)
    assert signals.measured["annual_share_growth"] == pytest.approx(3.0)


def test_the_healthy_twin_with_identical_prices_is_not_excluded(
    connection: Connection, healthy_twin: UUID
):
    """The control that makes the test mean something.

    Same price series, same base, opposite verdict — so the exclusion is
    demonstrably driven by fundamentals rather than by the shape of the
    chart, which is the entire claim the gate makes.
    """
    signals = load_distress_signals(connection, [healthy_twin], AS_OF)[healthy_twin]
    result = evaluate_bankruptcy_gate(signals, EligibilityParameters())

    assert result.passed
    assert signals.signal_count < 2


def test_the_healthy_twin_burning_cash_trips_only_one_signal(
    connection: Connection, healthy_twin: UUID
):
    """A well-capitalised company burning cash is not dying.

    It has 30 quarters of runway, so not even the runway signal fires —
    which is the calibration that keeps clinical-stage names in the pool.
    """
    signals = load_distress_signals(connection, [healthy_twin], AS_OF)[healthy_twin]
    assert "negative_shareholders_equity" not in signals.fired
    assert signals.measured["runway_quarters"] == pytest.approx(30.0)


def test_both_securities_produce_the_same_price_features(
    connection: Connection, dying_company: UUID, healthy_twin: UUID
):
    """Proves the fixtures really are indistinguishable from price alone.

    Without this the healthy/dying comparison could be passing because the
    two price series differ, and the gate would be untested.
    """
    features = compute_features_batch(connection, [dying_company, healthy_twin], AS_OF)
    dying = features.vectors[dying_company].features
    healthy = features.vectors[healthy_twin].features

    for name in ("drawdown_pct", "normalized_range_width", "volatility_compression"):
        assert dying[name] == pytest.approx(healthy[name])


# --------------------------------------------------------------------------
# Point-in-time correctness of the judgement itself
# --------------------------------------------------------------------------


def test_fundamentals_filed_after_the_as_of_are_invisible(
    connection: Connection, dying_company: UUID
):
    """A bankruptcy verdict made with future data is itself a leakage vector.

    Queried the day before the statements were filed, none of them are
    knowable — so no signal can fire and the gate passes, recording that
    it could not evaluate. That is the correct point-in-time answer even
    though the company was, in hindsight, already failing.
    """
    before_filing = FILED - timedelta(days=1)
    signals = load_distress_signals(connection, [dying_company], before_filing)[dying_company]

    assert signals.fired == ()
    assert evaluate_bankruptcy_gate(signals, EligibilityParameters()).passed

    # None of the distress-bearing quantities are even measured, because
    # the statements carrying them were not filed yet.
    for concept in ("shareholders_equity", "runway_quarters", "debt_to_assets"):
        assert concept not in signals.measured

    # `evaluated` is still True, and correctly so: the prior year's income
    # statement WAS knowable on this date. "Some fundamentals existed" and
    # "the ones that indicate distress existed" are different facts, and
    # `evaluated` reports only the first.
    assert signals.evaluated


def test_the_dilution_comparison_uses_the_statement_knowable_a_year_ago(
    connection: Connection, dying_company: UUID
):
    """Not a restated history — the share count ARGUS could have seen then.

    The comparison is between the latest knowable statement and the one
    that was latest a year earlier, both resolved through the same
    availability filter.
    """
    signals = load_distress_signals(connection, [dying_company], AS_OF)[dying_company]
    assert signals.measured["annual_share_growth"] == pytest.approx(3.0)

    # A year earlier both lookups resolve to the same filing — nothing
    # newer had been filed — so no growth is reported at all. Reporting
    # 0.0 there would assert "no dilution" where the truth is "no new
    # information", which is the failure this comparison guards against.
    earlier = load_distress_signals(connection, [dying_company], PRIOR_FILED)[dying_company]
    assert "annual_share_growth" not in earlier.measured


def test_unrecognised_field_names_surface_as_unevaluated_signals(
    connection: Connection, register, add_statement
):
    """The field-naming caveat, made checkable.

    Balance-sheet and cash-flow field names are unverified against a live
    FMP response (no fixture, egress blocked). If the real payload spells
    them differently, the concepts must come back *unmeasured* — visible
    in `detail` — rather than defaulting to zero and manufacturing
    distress out of a schema mismatch.
    """
    security_id = register("ODDKEYS")
    add_statement(
        security_id,
        CanonicalStatementType.BALANCE_SHEET,
        period_end=PERIOD_END,
        available=FILED,
        data={"some_unexpected_key": 1, "another": 2},
    )

    signals = load_distress_signals(connection, [security_id], AS_OF)[security_id]

    assert signals.evaluated is True
    assert signals.fired == ()
    assert "shareholders_equity" not in signals.measured
    assert "debt_to_assets" not in signals.measured


def test_a_missing_cash_balance_is_not_treated_as_zero_cash(
    connection: Connection, register, add_statement
):
    """Zero cash would fire the runway signal. Absent cash must not.

    The single most dangerous coercion in this gate: `None` becoming `0.0`
    turns every company with an incomplete filing into one with no money.
    """
    security_id = register("NOCASH")
    add_statement(
        security_id,
        CanonicalStatementType.BALANCE_SHEET,
        period_end=PERIOD_END,
        available=FILED,
        data={"totalAssets": 50_000_000, "totalStockholdersEquity": 30_000_000},
    )
    add_statement(
        security_id,
        CanonicalStatementType.CASH_FLOW,
        period_end=PERIOD_END,
        available=FILED,
        data={"operatingCashFlow": -5_000_000},
    )

    signals = load_distress_signals(connection, [security_id], AS_OF)[security_id]
    assert "short_cash_runway" not in signals.fired
    assert "runway_quarters" not in signals.measured
