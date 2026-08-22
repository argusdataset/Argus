"""A synthetic security that walks the entire ARGUS setup, end to end.

The feature-group tests need a price history where the *answer is known*:
a security that visibly declines, stabilizes, coils, wakes up and breaks
out, so that "does `volatility_compression` actually fall during the
consolidation" is a checkable question rather than a matter of opinion.

Synthetic rather than a real historical case, deliberately. A real ticker
would test two things at once — whether the features behave correctly and
whether my recollection of that ticker's 2018 chart is accurate — and a
failure would not distinguish them. Here every phase's parameters are
written down explicitly, so a direction test that fails is a statement
about the feature, not about the fixture.

The generator is seeded, so the same panel is produced on every run and a
direction assertion cannot pass by luck on one machine and fail on
another.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

import numpy as np
import pandas as pd

from core.feature_engine.panel import PricePanel

#: Deterministic namespace so security IDs are stable across runs — a
#: fixture that changed identity between runs would make a failure
#: impossible to reproduce.
_NAMESPACE = UUID("b5c1a7e2-0000-4000-8000-000000000000")

LIFECYCLE_ID = uuid5(_NAMESPACE, "lifecycle")
FLATLINE_ID = uuid5(_NAMESPACE, "flatline")
MARKET_ID = uuid5(_NAMESPACE, "market")

SEED = 20260822


@dataclass(frozen=True, slots=True)
class Phase:
    """One leg of the setup: where price ends up, how noisy, how busy.

    `noise` is a fractional daily amplitude, not an absolute price move,
    so the same phase definition would work on a $2 stock or a $2,000 one
    — the same scale-free discipline the features themselves follow.
    """

    name: str
    bars: int
    end_price: float
    noise: float
    volume: float


#: The full lifecycle. Prices and volumes are interpolated between each
#: phase's endpoint and the next, so transitions are gradual rather than
#: step changes — a step change would make every "did this rise?" test
#: pass trivially.
PHASES: tuple[Phase, ...] = (
    # A long prior advance, so there is a real structural peak to decline from.
    Phase("prior_advance", 300, end_price=200.0, noise=0.012, volume=1_000_000),
    # The decline: deep, fast, loud.
    Phase("decline", 130, end_price=95.0, noise=0.030, volume=2_400_000),
    # Stabilization: still drifting down, but the violence is draining out.
    Phase("stabilization", 50, end_price=90.0, noise=0.017, volume=1_300_000),
    # The base: flat, quiet, thin.
    Phase("consolidation", 130, end_price=90.0, noise=0.005, volume=550_000),
    # Awakening: range widens, volume returns, price presses the highs.
    Phase("awakening", 30, end_price=97.0, noise=0.014, volume=1_500_000),
    # Confirmation: through the level on expanding volume.
    Phase("confirmation", 25, end_price=135.0, noise=0.011, volume=5_000_000),
)

#: Where the market benchmark starts and ends over the same span. Rising
#: throughout, so the lifecycle security's relative strength genuinely
#: deteriorates during its decline rather than merely tracking a falling
#: market.
MARKET_START = 100.0
MARKET_END = 190.0

TOTAL_BARS = sum(phase.bars for phase in PHASES)


def phase_end_indices() -> dict[str, int]:
    """Positional index of each phase's final bar.

    Direction tests read features at these rows: "compare the reading at
    the end of the consolidation with the reading at the end of the
    decline" is the exact shape of every assertion.
    """
    indices: dict[str, int] = {}
    cursor = 0
    for phase in PHASES:
        cursor += phase.bars
        indices[phase.name] = cursor - 1
    return indices


def _piecewise(attribute: str, start_value: float) -> np.ndarray:
    """Linear interpolation of a per-phase attribute across every bar."""
    segments = [np.array([start_value])]
    previous = start_value
    for phase in PHASES:
        target = float(getattr(phase, attribute))
        segments.append(np.linspace(previous, target, phase.bars + 1)[1:])
        previous = target
    return np.concatenate(segments)[1:]


def build_lifecycle_panel() -> tuple[PricePanel, dict[str, int]]:
    """A three-security panel: the lifecycle, a flatline control, the market.

    The flatline control exists so a direction test cannot pass because a
    feature moved for *every* column — if `volatility_compression` fell
    for the flatline too, the feature is measuring the calendar, not the
    security.
    """
    rng = np.random.default_rng(SEED)
    dates = pd.bdate_range("2018-01-01", periods=TOTAL_BARS, tz="UTC")

    trend = _piecewise("end_price", PHASES[0].end_price / 2.0)
    noise = _piecewise("noise", PHASES[0].noise)
    volume_level = _piecewise("volume", PHASES[0].volume)

    columns = {
        LIFECYCLE_ID: _series(rng, trend, noise, volume_level),
        FLATLINE_ID: _series(
            rng,
            np.full(TOTAL_BARS, 40.0),
            np.full(TOTAL_BARS, 0.010),
            np.full(TOTAL_BARS, 800_000.0),
        ),
        MARKET_ID: _series(
            rng,
            np.linspace(MARKET_START, MARKET_END, TOTAL_BARS),
            np.full(TOTAL_BARS, 0.007),
            np.full(TOTAL_BARS, 90_000_000.0),
        ),
    }

    def frame(field: str) -> pd.DataFrame:
        return pd.DataFrame(
            {security_id: values[field] for security_id, values in columns.items()},
            index=dates,
        )

    close = frame("close")
    panel = PricePanel(
        open_adj=frame("open"),
        high_adj=frame("high"),
        low_adj=frame("low"),
        close_adj=close,
        close_raw=close.copy(),
        volume=frame("volume"),
        availability=pd.Series(
            {security_id: datetime(2026, 1, 1, tzinfo=UTC) for security_id in columns},
        ),
    )
    return panel, phase_end_indices()


def _series(
    rng: np.random.Generator,
    trend: np.ndarray,
    noise: np.ndarray,
    volume_level: np.ndarray,
) -> dict[str, np.ndarray]:
    """One security's OHLCV arrays from a trend/noise/volume specification.

    Close jitters around the trend rather than random-walking away from
    it, which keeps each phase's realized volatility governed by that
    phase's `noise` — the property every direction assertion depends on.
    """
    close = trend * (1.0 + rng.standard_normal(len(trend)) * noise)
    close = np.maximum(close, 0.01)

    previous_close = np.concatenate([[close[0]], close[:-1]])
    open_ = previous_close * (1.0 + rng.standard_normal(len(trend)) * noise * 0.3)

    body_high = np.maximum(open_, close)
    body_low = np.minimum(open_, close)
    high = body_high * (1.0 + np.abs(rng.standard_normal(len(trend))) * noise * 0.5)
    low = body_low * (1.0 - np.abs(rng.standard_normal(len(trend))) * noise * 0.5)

    volume = volume_level * (1.0 + rng.standard_normal(len(trend)) * 0.15)
    return {
        "open": open_,
        "high": high,
        "low": np.maximum(low, 0.01),
        "close": close,
        "volume": np.maximum(volume, 1.0),
    }
