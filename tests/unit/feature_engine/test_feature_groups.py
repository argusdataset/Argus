"""Do the features actually move the way the setup says they should?

A feature that computes without raising is not a working feature. The
only test that means anything is whether the number *tracks the thing it
claims to measure* — so every assertion here reads a feature at two
points in the synthetic lifecycle and asserts the direction of the change,
rather than pinning a magic value that would break the moment a window
changed.

Each test also checks the flatline control where the comparison is
meaningful. If `realized_volatility` fell for the flat security too, the
feature would be measuring the passage of time, not the security.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from core.feature_engine.groups import awakening, confirmation, consolidation, context, decline
from core.feature_engine.panel import PricePanel
from core.feature_engine.spec import (
    FEATURE_GROUPS,
    GROUP_A_FEATURES,
    GROUP_B_FEATURES,
    GROUP_C_FEATURES,
    GROUP_D_FEATURES,
    GROUP_E_FEATURES,
    FeatureSpec,
)
from tests.unit.feature_engine.lifecycle import FLATLINE_ID, LIFECYCLE_ID


def read(frames: dict[str, pd.DataFrame], name: str, row: int, security_id=LIFECYCLE_ID) -> float:
    """One feature's value at one bar for one security."""
    value = frames[name].iloc[row][security_id]
    assert not math.isnan(value), f"{name} is NaN at row {row} — the fixture is too short"
    return float(value)


# --------------------------------------------------------------------------
# Group A — Prior Decline & Stabilization
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def group_a(panel: PricePanel, spec: FeatureSpec, market_close: pd.Series):
    return decline.compute(panel, spec, market_close=market_close)


def test_group_a_covers_exactly_its_declared_features(group_a):
    assert set(group_a) == set(GROUP_A_FEATURES)


def test_decline_features_register_the_actual_decline(group_a, phase):
    """The core Group A claim: a fall from 200 to 95 is visible as one."""
    depth = read(group_a, "peak_to_trough_decline", phase["decline"])
    drawdown = read(group_a, "drawdown_pct", phase["decline"])

    assert depth < -0.4, "a >50% fall should read as a deep peak-to-trough decline"
    assert drawdown < -0.4
    # Sign convention: both are negative fractions, never absolute values —
    # a downstream module must be able to tell direction from the number.
    assert depth <= drawdown or drawdown <= 0


def test_the_flatline_control_shows_no_decline(group_a, phase):
    """Same window, same bars — a security that did not fall must not read as falling."""
    drawdown = read(group_a, "drawdown_pct", phase["decline"], FLATLINE_ID)
    assert drawdown > -0.15


def test_decline_duration_is_measured_not_gated(group_a, phase):
    """Duration is reported as a number, and it grows as the decline runs on.

    The whole point of storing it: nothing here branches on it, so a
    130-bar decline and a 13-bar one both produce valid vectors — the
    number simply differs.
    """
    early = read(group_a, "decline_duration_bars", phase["prior_advance"] + 40)
    late = read(group_a, "decline_duration_bars", phase["decline"])
    assert late > early
    assert late >= 100, "bars since the structural peak should span most of the decline"


def test_decline_speed_combines_depth_and_duration(group_a, phase):
    """A decline's speed is its depth divided by the bars it took."""
    depth = read(group_a, "peak_to_trough_decline", phase["decline"])
    duration = read(group_a, "decline_duration_bars", phase["decline"])
    speed = read(group_a, "decline_speed", phase["decline"])
    assert speed == pytest.approx(depth / duration, rel=1e-9)
    assert speed < 0


def test_new_lows_become_rarer_as_the_decline_stabilizes(group_a, phase):
    """`lower_low_frequency` falling is the signature of a decline losing force."""
    during = read(group_a, "lower_low_frequency", phase["decline"])
    after = read(group_a, "lower_low_frequency", phase["consolidation"])
    assert after < during

    # And the derived feature says the same thing with the opposite sign.
    reduction = read(group_a, "downside_momentum_reduction", phase["stabilization"])
    assert reduction > 0


def test_volatility_contraction_onset_falls_below_one_as_the_fall_ends(group_a, phase):
    """Ratio of recent to prior volatility: below 1 means contraction started."""
    stabilizing = read(group_a, "volatility_contraction_onset", phase["stabilization"])
    assert stabilizing < 1.0


def test_relative_strength_deteriorates_against_a_rising_market(group_a, phase):
    """The market rose throughout, so the decline is a genuine RS loss.

    This assertion found a real bug — see `decline._relative_damage`. The
    feature originally second-differenced an unnormalized slope and read
    *positive* here, at the bottom of a 55% relative-strength collapse.
    """
    assert read(group_a, "rs_deterioration_vs_market", phase["decline"]) < -0.3
    # And it stops reading as damage once the security has recovered,
    # rather than staying permanently negative.
    assert read(group_a, "rs_deterioration_vs_market", phase["confirmation"]) > read(
        group_a, "rs_deterioration_vs_market", phase["consolidation"]
    )


def test_a_missing_sector_benchmark_is_nan_not_zero(group_a, phase):
    """No sector data exists in ARGUS — the feature must say so, not say 'flat'.

    Zero would be indistinguishable from "measured, and unchanged". NaN
    (and the `MissReason` the engine attaches alongside it) is the only
    honest encoding.
    """
    value = group_a["rs_deterioration_vs_sector"].iloc[phase["decline"]][LIFECYCLE_ID]
    assert math.isnan(value)


# --------------------------------------------------------------------------
# Group B — Consolidation
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def group_b(panel: PricePanel, spec: FeatureSpec):
    return consolidation.compute(panel, spec)


def test_group_b_covers_exactly_its_declared_features(group_b):
    assert set(group_b) == set(GROUP_B_FEATURES)


def test_volatility_falls_monotonically_from_decline_into_the_base(group_b, phase):
    """decline → stabilization → consolidation is a descending volatility chain."""
    during_decline = read(group_b, "realized_volatility", phase["decline"])
    stabilizing = read(group_b, "realized_volatility", phase["stabilization"])
    in_the_base = read(group_b, "realized_volatility", phase["consolidation"])

    assert during_decline > stabilizing > in_the_base


def test_the_range_tightens_in_the_base(group_b, phase):
    """`normalized_range_width` is the most direct statement of 'coiling'."""
    during_decline = read(group_b, "normalized_range_width", phase["decline"])
    in_the_base = read(group_b, "normalized_range_width", phase["consolidation"])
    assert in_the_base < during_decline / 2


def test_atr_percentile_places_the_base_at_the_low_end_of_its_own_history(group_b, phase):
    """Self-relative, so it is comparable across securities and price levels."""
    in_the_base = read(group_b, "atr_percentile", phase["consolidation"])
    assert 0.0 <= in_the_base <= 1.0
    assert in_the_base < 0.25


def test_volume_contracts_into_the_base_and_expands_out_of_it(group_b, phase):
    """Thin in the base, heavier once it wakes up — both sides of the claim."""
    assert read(group_b, "volume_contraction", phase["consolidation"]) < 1.0
    assert read(group_b, "volume_contraction", phase["confirmation"]) > 1.0


def test_structure_transition_rises_through_zero_out_of_the_base(group_b, phase):
    """A measured trend, not a bearish/neutral/bullish label.

    Negative while price is still falling, positive once it is advancing —
    and the intermediate values are real numbers, which is exactly what a
    three-valued flag would have thrown away.
    """
    falling = read(group_b, "structure_transition", phase["decline"])
    rising = read(group_b, "structure_transition", phase["confirmation"])
    assert falling < 0 < rising


def test_higher_low_development_is_the_magnitude_sensitive_companion(group_b, phase):
    """The slope of the rolling low, rising as the base builds.

    Asserted here because `test_higher_highs_and_higher_lows_become_more_frequent`
    leans on it: Group C's `higher_low_frequency` partly tracks quietness,
    and this is the feature that carries the magnitude of rising lows.
    Negative through the decline, through zero in the base, clearly
    positive out of it.
    """
    assert read(group_b, "higher_low_development", phase["decline"]) < 0
    assert (
        read(group_b, "higher_low_development", phase["stabilization"])
        < read(group_b, "higher_low_development", phase["consolidation"])
        < read(group_b, "higher_low_development", phase["awakening"])
        < read(group_b, "higher_low_development", phase["confirmation"])
    )


def test_range_edges_get_tested_while_price_coils(group_b, phase):
    """A base is defined by repeated visits to its edges."""
    supports = read(group_b, "support_test_count", phase["consolidation"])
    resistances = read(group_b, "resistance_test_count", phase["consolidation"])
    assert supports > 0
    assert resistances > 0


# --------------------------------------------------------------------------
# Group C — Awakening
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def group_c(panel: PricePanel, spec: FeatureSpec, market_close: pd.Series):
    return awakening.compute(panel, spec, market_close=market_close)


def test_group_c_covers_exactly_its_declared_features(group_c):
    assert set(group_c) == set(GROUP_C_FEATURES)


def test_volatility_and_volume_re_expand_when_the_base_wakes_up(group_c, phase):
    """Group C is the mirror of Group B: the same quantities, turning back up."""
    quiet = read(group_c, "volatility_reexpansion", phase["consolidation"])
    waking = read(group_c, "volatility_reexpansion", phase["awakening"])
    assert waking > quiet
    assert waking > 1.0

    assert read(group_c, "volume_expansion", phase["awakening"]) > read(
        group_c, "volume_expansion", phase["consolidation"]
    )


def test_bars_get_wider_as_the_setup_activates(group_c, phase):
    assert read(group_c, "range_expansion", phase["awakening"]) > 1.0
    assert read(group_c, "range_expansion", phase["consolidation"]) < read(
        group_c, "range_expansion", phase["awakening"]
    )


def test_price_presses_the_top_of_its_range(group_c, phase):
    """`resistance_pressure` is continuous — a magnitude, not 'near resistance: yes'."""
    coiling = read(group_c, "resistance_pressure", phase["consolidation"])
    pressing = read(group_c, "resistance_pressure", phase["awakening"])
    assert pressing > coiling
    assert pressing > 0.7
    assert read(group_c, "time_in_upper_range", phase["awakening"]) > read(
        group_c, "time_in_upper_range", phase["consolidation"]
    )


def test_higher_highs_and_higher_lows_become_more_frequent(group_c, phase):
    """Both compared against the decline, and only one against the base.

    `higher_low_frequency` in the base (0.90) is *higher* than in the
    awakening (0.80), which looks wrong and is not. It is a frequency, not
    a magnitude: in a very quiet range almost every bar's low clears the
    prior 5-bar low simply because nothing moves far, while a livelier
    advance prints deeper pullbacks. Both readings are true statements
    about how often a higher low occurred.

    Recorded rather than tuned away, because a downstream module weighing
    `higher_low_frequency` needs to know it partly tracks quietness.
    Group B's `higher_low_development` — the slope of the rolling low — is
    the magnitude-sensitive companion, and it does rise out of the base.
    """
    for name in ("higher_high_frequency", "higher_low_frequency"):
        assert read(group_c, name, phase["awakening"]) > read(group_c, name, phase["decline"])

    assert read(group_c, "higher_high_frequency", phase["awakening"]) > read(
        group_c, "higher_high_frequency", phase["consolidation"]
    )


def test_rs_improvement_is_observable_without_a_breakout(group_c, phase):
    """The design point from the module brief: RS leads price.

    Measured at the *end of the awakening* — before the confirmation phase
    breaks the range. If this feature required a breakout to register, the
    leading behaviour it exists to capture would be unobservable by
    construction.
    """
    before_the_breakout = read(group_c, "rs_improvement_vs_market", phase["awakening"])
    in_the_base = read(group_c, "rs_improvement_vs_market", phase["consolidation"])
    assert before_the_breakout > in_the_base


# --------------------------------------------------------------------------
# Group D — Confirmation
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def group_d(panel: PricePanel, spec: FeatureSpec, market_close: pd.Series):
    return confirmation.compute(panel, spec, market_close=market_close)


def test_group_d_covers_exactly_its_declared_features(group_d):
    assert set(group_d) == set(GROUP_D_FEATURES)


def test_the_breakout_registers_as_a_positive_magnitude(group_d, phase):
    """Negative while below the level, positive through it — never a boolean.

    A downstream module can threshold a magnitude however it likes; it
    could not recover one from a `has_broken_out` flag.
    """
    coiling = read(group_d, "resistance_breakout_pct", phase["consolidation"])
    breaking = read(group_d, "resistance_breakout_pct", phase["confirmation"])
    assert coiling < 0
    assert breaking > 0


def test_the_breakout_carries_volume(group_d, phase):
    assert read(group_d, "breakout_volume_ratio", phase["confirmation"]) > 1.2


def test_acceptance_distinguishes_a_held_breakout_from_a_one_bar_spike(group_d, phase):
    """Fraction of recent bars closing above the level, not 'it broke once'."""
    held = read(group_d, "acceptance_followthrough", phase["confirmation"])
    coiling = read(group_d, "acceptance_followthrough", phase["consolidation"])
    assert coiling < 0.15, "a coiling security rarely closes above its own range high"
    assert held > 0.3, "a held breakout closes above the level repeatedly, not once"
    assert held > coiling


def test_higher_high_magnitude_measures_how_decisively(group_d, phase):
    assert read(group_d, "higher_high_magnitude", phase["confirmation"]) > 0
    assert read(group_d, "higher_high_magnitude", phase["decline"]) < 0


def test_rs_alignment_is_positive_when_price_and_rs_agree(group_d, phase):
    """Price rising *and* outperforming — the product of two slopes."""
    assert read(group_d, "rs_alignment_market", phase["confirmation"]) > 0


# --------------------------------------------------------------------------
# Group E — Context
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def group_e(panel: PricePanel, spec: FeatureSpec, market_close: pd.Series):
    return context.compute(panel, spec, market_close=market_close)


def test_group_e_covers_exactly_its_declared_features(group_e):
    assert set(group_e) == set(GROUP_E_FEATURES)


def test_relative_strength_tracks_the_gap_against_the_market(group_e, phase):
    assert read(group_e, "rs_vs_market", phase["decline"]) < 0
    assert read(group_e, "rs_vs_market", phase["confirmation"]) > 0


def test_sector_and_industry_relative_strength_are_nan_without_a_mapping(group_e, phase):
    """ARGUS stores no sector data. Two features therefore cannot compute.

    Asserted rather than quietly tolerated, because the failure mode this
    guards against is a future change that "fixes" the NaN by substituting
    the market benchmark and silently redefines what `rs_vs_sector` means.
    """
    for name in ("rs_vs_sector", "rs_vs_industry"):
        assert math.isnan(group_e[name].iloc[phase["confirmation"]][LIFECYCLE_ID])


def test_market_regime_features_are_inputs_not_a_label(group_e, phase):
    """Continuous measurements of the benchmark — never a regime name.

    Module 10 owns `market_state`. If this module emitted a label, ARGUS
    would have two sources of truth for the same question.
    """
    trend = read(group_e, "market_regime_trend", phase["confirmation"])
    volatility = read(group_e, "market_regime_volatility", phase["confirmation"])
    drawdown = read(group_e, "market_regime_drawdown", phase["confirmation"])

    assert isinstance(trend, float)
    assert trend > 0, "the synthetic market rises throughout"
    assert volatility > 0
    assert drawdown <= 0


def test_market_regime_is_identical_across_securities_on_a_given_bar(group_e, phase):
    """Regime is a property of the market, broadcast to every column."""
    row = group_e["market_regime_trend"].iloc[phase["confirmation"]]
    assert row.nunique(dropna=True) == 1


def test_dollar_volume_uses_raw_price_so_splits_do_not_understate_it(panel, spec, phase):
    """Liquidity is a statement about the day, in the day's actual dollars."""
    frames = context.compute(panel, spec)
    value = read(frames, "avg_dollar_volume", phase["consolidation"])
    expected = (
        (panel.close_raw * panel.volume)
        .rolling(spec.windows.medium)
        .mean()
        .iloc[phase["consolidation"]][LIFECYCLE_ID]
    )
    assert value == pytest.approx(expected)


# --------------------------------------------------------------------------
# The list as a whole
# --------------------------------------------------------------------------


def test_every_declared_feature_is_produced_by_exactly_one_group(
    group_a, group_b, group_c, group_d, group_e
):
    """No feature is silently missing, and none is computed twice.

    Two groups producing the same name would mean the later one wins in
    `_compute_all_groups`'s dict merge — a value quietly overwritten by
    whichever module happened to run last.
    """
    produced: dict[str, str] = {}
    for group_name, frames in zip(
        FEATURE_GROUPS, (group_a, group_b, group_c, group_d, group_e), strict=True
    ):
        for name in frames:
            assert name not in produced, (
                f"{name} produced by both {produced[name]} and {group_name}"
            )
            produced[name] = group_name

    for group_name, names in FEATURE_GROUPS.items():
        for name in names:
            assert produced.get(name) == group_name
