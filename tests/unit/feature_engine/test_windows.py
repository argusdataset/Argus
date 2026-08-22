"""The vectorized rolling helpers, against hand-computable answers.

These are the primitives every feature is built on, and three of them
(`window_min_after_max`, `rolling_slope`, `rolling_percentile_rank`) are
non-obvious enough that "it ran without raising" says nothing. Each test
here uses a series small enough to work out on paper, so a wrong answer is
visibly wrong rather than merely different.

`window_min_after_max` gets the most attention because it encodes what
"peak to trough" actually means: a trough *before* the peak is not a
decline from it, and the naive `rolling().min()` version silently returns
one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.feature_engine.windows import (
    bars_since_window_max,
    bars_since_window_min,
    rolling_percentile_rank,
    rolling_slope,
    safe_ratio,
    window_min_after_max,
)


def frame(*columns: list[float]) -> pd.DataFrame:
    """A wide frame — dates down, securities across — from plain lists."""
    return pd.DataFrame(
        {f"s{i}": values for i, values in enumerate(columns)},
        index=pd.bdate_range("2020-01-01", periods=len(columns[0]), tz="UTC"),
    )


def test_bars_since_window_max_is_zero_on_the_peak_itself():
    data = frame([1.0, 5.0, 2.0, 3.0])
    result = bars_since_window_max(data, 4)
    assert result.iloc[3]["s0"] == 2.0  # the 5.0 was two bars ago


def test_bars_since_window_max_counts_from_the_current_bar():
    data = frame([1.0, 2.0, 3.0, 9.0])
    assert bars_since_window_max(data, 4).iloc[3]["s0"] == 0.0


def test_bars_since_window_min_mirrors_it():
    data = frame([9.0, 1.0, 3.0, 4.0])
    assert bars_since_window_min(data, 4).iloc[3]["s0"] == 2.0


def test_windows_shorter_than_the_frame_are_nan_padded_at_the_front():
    """No partial window is ever reported as if it were a full one."""
    data = frame([1.0, 2.0, 3.0, 4.0])
    result = bars_since_window_max(data, 3)
    assert result.iloc[:2]["s0"].isna().all()
    assert result.iloc[2:]["s0"].notna().all()


def test_window_min_after_max_ignores_a_trough_before_the_peak():
    """The whole reason this helper exists rather than `rolling().min()`.

    Series: a low of 1 *before* a peak of 10, then a decline to 4. The
    peak-to-trough decline is 10 → 4, not 10 → 1 — the 1 happened before
    the peak and is not a fall from it.
    """
    data = frame([5.0, 1.0, 10.0, 7.0, 4.0])
    assert window_min_after_max(data, 5).iloc[4]["s0"] == 4.0
    # The naive version, for contrast — this is the wrong answer.
    assert data.rolling(5).min().iloc[4]["s0"] == 1.0


def test_window_min_after_max_falls_back_to_the_peak_when_it_is_the_last_bar():
    """Nothing came after the peak, so there is no trough to report.

    Returning the peak itself makes `peak_to_trough_decline` exactly zero
    — "it has not declined from here yet" — which is the only defensible
    reading. A NaN would be indistinguishable from insufficient history.
    """
    data = frame([1.0, 2.0, 3.0, 9.0])
    assert window_min_after_max(data, 4).iloc[3]["s0"] == 9.0


def test_window_min_after_max_covers_every_security_in_one_pass():
    """Two securities with different peak positions, one call."""
    data = frame([5.0, 1.0, 10.0, 7.0, 4.0], [10.0, 8.0, 2.0, 6.0, 3.0])
    result = window_min_after_max(data, 5).iloc[4]
    assert result["s0"] == 4.0  # peak at index 2, min after it is 4
    assert result["s1"] == 2.0  # peak at index 0, min after it is 2


def test_rolling_percentile_rank_places_the_current_value_in_its_own_history():
    data = frame([1.0, 2.0, 3.0, 4.0, 5.0])
    # 5.0 is at or above all five values in the window.
    assert rolling_percentile_rank(data, 5).iloc[4]["s0"] == 1.0

    falling = frame([5.0, 4.0, 3.0, 2.0, 1.0])
    # 1.0 is at or above only itself: 1/5.
    assert falling.pipe(rolling_percentile_rank, 5).iloc[4]["s0"] == pytest.approx(0.2)


def test_rolling_percentile_rank_is_bounded():
    rng = np.random.default_rng(7)
    data = frame(list(rng.standard_normal(200)))
    ranks = rolling_percentile_rank(data, 60).dropna()
    assert ranks.to_numpy().min() > 0.0
    assert ranks.to_numpy().max() <= 1.0


def test_rolling_slope_recovers_a_known_gradient():
    """A perfectly linear series of gradient 3 must yield exactly 3."""
    data = frame([1.0, 4.0, 7.0, 10.0, 13.0])
    assert rolling_slope(data, 5).iloc[4]["s0"] == pytest.approx(3.0)


def test_rolling_slope_agrees_with_polyfit():
    """The closed-form OLS is the same arithmetic, just without the loop.

    Worth checking explicitly: the closed form is what makes the batch
    path vectorized, and a subtle algebra slip there would be invisible
    in every downstream feature.
    """
    rng = np.random.default_rng(11)
    values = list(rng.standard_normal(80).cumsum())
    data = frame(values)

    window = 20
    computed = rolling_slope(data, window)
    for position in (25, 50, 79):
        segment = np.array(values[position - window + 1 : position + 1])
        expected = np.polyfit(np.arange(window), segment, 1)[0]
        assert computed.iloc[position]["s0"] == pytest.approx(expected, rel=1e-9)


def test_rolling_slope_rejects_a_degenerate_window():
    with pytest.raises(ValueError, match="at least 2 bars"):
        rolling_slope(frame([1.0, 2.0]), 1)


def test_safe_ratio_turns_a_zero_denominator_into_nan_not_inf():
    """`inf` would read as an extreme measurement rather than a missing one.

    A downstream module ranking on `volatility_compression` would sort an
    `inf` to the top of the list — a security with no measurable prior
    volatility presented as the strongest reading in the universe.
    """
    numerator = frame([1.0, 2.0])
    denominator = frame([0.0, 4.0])
    result = safe_ratio(numerator, denominator)
    assert np.isnan(result.iloc[0]["s0"])
    assert result.iloc[1]["s0"] == pytest.approx(0.5)


def test_helpers_return_all_nan_rather_than_raising_on_short_history():
    """A recently-listed security is a normal case, not an error case."""
    data = frame([1.0, 2.0])
    for helper in (
        bars_since_window_max,
        bars_since_window_min,
        window_min_after_max,
        rolling_percentile_rank,
    ):
        result = helper(data, 60)
        assert result.shape == data.shape
        assert result.isna().all().all()
