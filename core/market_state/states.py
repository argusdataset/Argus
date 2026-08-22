"""State predicates, resolution precedence, and the derived watchlists.

Each of the nine states gets one named predicate over Module 08's
features, and the features it reads are declared alongside it. Declaring
the inputs is not bookkeeping: it is what lets the engine refuse to
classify a security whose evidence is too thin for *that particular*
state, rather than applying one global data-sufficiency rule that would
be wrong for eight of the nine.

## Reading Group E rather than recomputing it

Module 09's report established that Group E already produces
`market_regime_trend` / `_volatility` / `_drawdown` as continuous inputs
carrying no classification. This module consumes them. Nothing here
recomputes a regime signal from prices — the raw measurement belongs to
Module 08, the classification belongs here, and duplicating the first
would give ARGUS two sources of truth for the same quantity.

## Precedence, and why it runs backwards

States are tested most-advanced first, and the first match wins. That
ordering is doing real work rather than being an implementation detail.

The states are not mutually exclusive as predicates: a security that has
just broken out on volume still satisfies "range is tight relative to
price" for a while, because the range measurement lags. Testing
CONSOLIDATION first would pin it there and the breakout would never be
seen. Testing UPTREND first resolves it correctly, and the general rule —
*the furthest point along the sequence whose evidence is satisfied* — is
the one that matches how the setup actually unfolds.

DISTRIBUTION is the exception and sits above UPTREND, because it is a
qualification of having been in an uptrend, not a stage beyond it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from core.market_state.thresholds import StateThresholds
from infra.db.enums import MarketState

#: The three official watchlists, as a mapping to the states they cover.
#:
#: Derived views over `market_state`, never stored tables — the
#: Source-of-Truth principle. `UPTREND` and `DISTRIBUTION` map to no
#: public list: they are internal states that inform transition logic and
#: same-asset history without being surfaced as their own watchlist.
WATCHLISTS: dict[str, frozenset[MarketState]] = {
    "DOWN_TREND": frozenset({MarketState.DOWN_TREND, MarketState.BASE_FORMING}),
    "CONSOLIDATION": frozenset({MarketState.CONSOLIDATION, MarketState.ACCUMULATION}),
    "BREAKOUT_READY": frozenset({MarketState.BREAKOUT_WATCH, MarketState.BREAKOUT_READY}),
}

#: States that appear on no public watchlist.
INTERNAL_STATES: frozenset[MarketState] = frozenset(
    {MarketState.UNCLASSIFIED, MarketState.UPTREND, MarketState.DISTRIBUTION}
)


@dataclass(frozen=True, slots=True)
class StateDefinition:
    """One state's entry criteria, declared inputs, and rationale.

    `inputs` exists so the engine can compute per-state evidence coverage
    and refuse to classify on fragments. `predicate` returns a boolean
    Series over the whole universe — never a per-security call.
    """

    state: MarketState
    inputs: tuple[str, ...]
    predicate: Callable[[pd.DataFrame, StateThresholds], pd.Series]
    rationale: str


def _at(frame: pd.DataFrame, name: str) -> pd.Series:
    """A feature column, or an all-NaN column when the feature is absent.

    Absent rather than raising: a universe where one feature failed to
    compute should degrade to UNCLASSIFIED for the affected securities,
    not abort the whole scan.
    """
    if name in frame.columns:
        return frame[name]
    return pd.Series(float("nan"), index=frame.index)


# --------------------------------------------------------------------------
# The nine predicates. Every numeric comparison reads from `t`.
# --------------------------------------------------------------------------


def _down_trend(frame: pd.DataFrame, t: StateThresholds) -> pd.Series:
    return (_at(frame, "structure_transition") < t.downtrend_structure.value) & (
        _at(frame, "lower_low_frequency") >= t.downtrend_lower_low_frequency.value
    )


def _base_forming(frame: pd.DataFrame, t: StateThresholds) -> pd.Series:
    return (_at(frame, "downside_momentum_reduction") > t.base_forming_downside_reduction.value) & (
        _at(frame, "volatility_contraction_onset") < t.base_forming_volatility_contraction.value
    )


def _consolidation(frame: pd.DataFrame, t: StateThresholds) -> pd.Series:
    return (
        (_at(frame, "volatility_compression") <= t.consolidation_max_volatility_expansion.value)
        & (_at(frame, "atr_percentile") <= t.consolidation_atr_percentile.value)
        & (_at(frame, "normalized_range_width") <= t.consolidation_range_width.value)
    )


def _accumulation(frame: pd.DataFrame, t: StateThresholds) -> pd.Series:
    """Consolidation plus rising lows and a defended floor."""
    return (
        _consolidation(frame, t)
        & (_at(frame, "higher_low_development") > t.accumulation_higher_low_development.value)
        & (_at(frame, "support_test_count") >= t.accumulation_support_tests.value)
    )


def _breakout_watch(frame: pd.DataFrame, t: StateThresholds) -> pd.Series:
    return (
        (_at(frame, "volatility_reexpansion") >= t.breakout_watch_volatility_reexpansion.value)
        & (_at(frame, "volume_expansion") >= t.breakout_watch_volume_expansion.value)
        & (_at(frame, "resistance_pressure") >= t.breakout_watch_resistance_pressure.value)
    )


def _breakout_ready(frame: pd.DataFrame, t: StateThresholds) -> pd.Series:
    """Pressing the level hard and sustained, but not yet through it."""
    return (
        (_at(frame, "resistance_pressure") >= t.breakout_ready_resistance_pressure.value)
        & (_at(frame, "time_in_upper_range") >= t.breakout_ready_time_in_upper_range.value)
        & (_at(frame, "resistance_breakout_pct") <= t.uptrend_breakout_pct.value)
    )


def _uptrend(frame: pd.DataFrame, t: StateThresholds) -> pd.Series:
    """Through the level and holding — not a one-bar spike."""
    return (_at(frame, "resistance_breakout_pct") > t.uptrend_breakout_pct.value) & (
        _at(frame, "acceptance_followthrough") >= t.uptrend_acceptance.value
    )


def _distribution(frame: pd.DataFrame, t: StateThresholds) -> pd.Series:
    """Topping: momentum decaying while still near the highs.

    The drawdown bound is what separates distribution from decline. Past
    it the security is falling, not topping, and DOWN_TREND is the honest
    reading.
    """
    return (
        (_at(frame, "momentum_improvement") < t.distribution_momentum.value)
        & (_at(frame, "drawdown_pct") <= t.distribution_momentum.value)
        & (_at(frame, "drawdown_pct") >= t.distribution_drawdown_from_high.value)
        & (_at(frame, "higher_high_frequency") < t.uptrend_acceptance.value)
    )


#: Evaluated in this order; first match wins. See the module docstring on
#: why the sequence runs from most-advanced backwards.
STATE_DEFINITIONS: tuple[StateDefinition, ...] = (
    StateDefinition(
        MarketState.DISTRIBUTION,
        ("momentum_improvement", "drawdown_pct", "higher_high_frequency"),
        _distribution,
        "Momentum decaying near the highs — topping rather than declining.",
    ),
    StateDefinition(
        MarketState.UPTREND,
        ("resistance_breakout_pct", "acceptance_followthrough"),
        _uptrend,
        "Through the resistance level and closing above it repeatedly.",
    ),
    StateDefinition(
        MarketState.BREAKOUT_READY,
        ("resistance_pressure", "time_in_upper_range", "resistance_breakout_pct"),
        _breakout_ready,
        "Sustained pressure at the top of the range, level not yet cleared.",
    ),
    StateDefinition(
        MarketState.BREAKOUT_WATCH,
        ("volatility_reexpansion", "volume_expansion", "resistance_pressure"),
        _breakout_watch,
        "Volatility and volume re-expanding, price moving into the upper range.",
    ),
    StateDefinition(
        MarketState.ACCUMULATION,
        (
            "volatility_compression",
            "atr_percentile",
            "normalized_range_width",
            "higher_low_development",
            "support_test_count",
        ),
        _accumulation,
        "A consolidation with rising lows and a repeatedly defended floor.",
    ),
    StateDefinition(
        MarketState.CONSOLIDATION,
        ("volatility_compression", "atr_percentile", "normalized_range_width"),
        _consolidation,
        "Coiled: compressed volatility, quiet ATR, tight range.",
    ),
    StateDefinition(
        MarketState.BASE_FORMING,
        ("downside_momentum_reduction", "volatility_contraction_onset"),
        _base_forming,
        "The decline losing force — new lows rarer, volatility contracting.",
    ),
    StateDefinition(
        MarketState.DOWN_TREND,
        ("structure_transition", "lower_low_frequency"),
        _down_trend,
        "Still falling and still making new lows.",
    ),
)

#: Every feature any predicate reads. Used to build the classifier's frame.
CLASSIFICATION_FEATURES: tuple[str, ...] = tuple(
    dict.fromkeys(name for definition in STATE_DEFINITIONS for name in definition.inputs)
)


def watchlist_for(state: MarketState) -> str | None:
    """Which public watchlist a state appears on, if any."""
    for name, states in WATCHLISTS.items():
        if state in states:
            return name
    return None
