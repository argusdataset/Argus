"""Vectorized rolling helpers operating across the whole universe at once.

Every function here takes a **wide** frame — index is dates, one column
per security — and returns a frame of the same shape. That layout is the
reason the batch path is genuinely vectorized: `close.rolling(20).mean()`
computes a 20-bar mean for ten thousand securities in one call, not ten
thousand calls.

A few features need `argmax`/`argmin` *within* a rolling window ("how
many bars since the window's peak"), which pandas can only do via
`.rolling().apply()` — a Python-level loop per window per column, and the
single easiest way to accidentally build a disguised loop. Those use
`numpy.lib.stride_tricks.sliding_window_view` instead, which materialises
every window as a view and lets numpy reduce over the whole
(time × security × window) block in one operation.

Memory note: `sliding_window_view` creates a view, not a copy, but the
reduction over it allocates a (T × N) result. For a universe-scale panel
the caller bounds `T` by loading only the longest lookback window it
needs (see `panel.py`), which keeps this comfortably in memory.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


def _as_windows(frame: pd.DataFrame, window: int) -> np.ndarray:
    """(T-window+1, N, window) view over a wide frame's values."""
    values = frame.to_numpy(dtype=float, copy=False)
    return sliding_window_view(values, window, axis=0)


def _rebuild(frame: pd.DataFrame, reduced: np.ndarray, window: int) -> pd.DataFrame:
    """Place a (T-window+1, N) reduction back on the original index, NaN-padded."""
    out = np.full(frame.shape, np.nan, dtype=float)
    out[window - 1 :] = reduced
    return pd.DataFrame(out, index=frame.index, columns=frame.columns)


def bars_since_window_max(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    """Bars elapsed since the highest value in the trailing window.

    0 means the current bar *is* the window's high. This is a duration
    *measurement* — a stored feature — never a condition anything branches
    on.
    """
    if len(frame) < window:
        return pd.DataFrame(np.nan, index=frame.index, columns=frame.columns)
    positions = _as_windows(frame, window).argmax(axis=-1)
    return _rebuild(frame, (window - 1) - positions, window)


def bars_since_window_min(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    """Bars elapsed since the lowest value in the trailing window."""
    if len(frame) < window:
        return pd.DataFrame(np.nan, index=frame.index, columns=frame.columns)
    positions = _as_windows(frame, window).argmin(axis=-1)
    return _rebuild(frame, (window - 1) - positions, window)


def window_min_after_max(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    """The lowest value occurring *after* the window's peak.

    This is what "peak to trough" actually means: a trough before the peak
    is not a decline from it. Computed by masking each window's values at
    and before its own argmax, then taking the minimum of what remains —
    vectorized over every security and every window position at once.
    """
    if len(frame) < window:
        return pd.DataFrame(np.nan, index=frame.index, columns=frame.columns)

    blocks = _as_windows(frame, window)
    peak_positions = blocks.argmax(axis=-1)[..., None]
    offsets = np.arange(window)[None, None, :]
    after_peak = np.where(offsets > peak_positions, blocks, np.inf)

    troughs = after_peak.min(axis=-1)
    # A window whose peak is its final bar has nothing after it; the peak
    # itself is then the only defensible value.
    peak_values = blocks.max(axis=-1)
    troughs = np.where(np.isfinite(troughs), troughs, peak_values)
    return _rebuild(frame, troughs, window)


def rolling_percentile_rank(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    """Where the current value sits within its own trailing distribution.

    Returns 0..1 — the fraction of the trailing window at or below the
    current value. Deliberately self-relative: an ATR percentile is
    meaningful against the security's own history, never against an
    absolute cutoff that would mean different things for a $2 stock and a
    $2,000 one.
    """
    if len(frame) < window:
        return pd.DataFrame(np.nan, index=frame.index, columns=frame.columns)

    blocks = _as_windows(frame, window)
    current = blocks[..., -1][..., None]
    ranks = (blocks <= current).sum(axis=-1) / window
    return _rebuild(frame, ranks, window)


def rolling_slope(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    """Least-squares slope per bar over the trailing window.

    Closed-form OLS rather than a per-window `polyfit` call: with `x`
    fixed at 0..window-1, the slope reduces to a few rolling sums, so the
    whole universe is covered by vectorized pandas operations.
    """
    if window < 2:
        raise ValueError("rolling_slope needs a window of at least 2 bars")

    x = np.arange(window, dtype=float)
    x_mean = x.mean()
    x_var = ((x - x_mean) ** 2).sum()

    y_mean = frame.rolling(window).mean()
    # cov(x, y) * window, via a rolling dot product against fixed weights.
    covariance = _rolling_dot(frame, x) - x.sum() * y_mean
    return covariance / x_var


def _rolling_dot(frame: pd.DataFrame, weights: np.ndarray) -> pd.DataFrame:
    """Rolling dot product against fixed weights, without a Python loop."""
    window = len(weights)
    if len(frame) < window:
        return pd.DataFrame(np.nan, index=frame.index, columns=frame.columns)
    blocks = _as_windows(frame, window)
    return _rebuild(frame, np.einsum("tnw,w->tn", blocks, weights), window)


def safe_ratio(numerator: pd.DataFrame, denominator: pd.DataFrame) -> pd.DataFrame:
    """Element-wise ratio with zero denominators becoming NaN, not inf.

    A zero denominator means the quantity is undefined, and NaN is how
    this module says "undefined". Letting an `inf` through would look like
    an extreme reading rather than a missing one, which is exactly the
    kind of quietly-wrong number the whole project is built to avoid.
    """
    denominator = denominator.replace(0.0, np.nan)
    return numerator / denominator
