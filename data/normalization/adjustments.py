"""Corporate action adjustment — producing the adjusted series.

ARGUS keeps two price series and needs both:

- **Raw** is what actually printed that day. It is the point-in-time
  reality a trader would have seen, and the only honest basis for "this
  would have worked". A pre-split Apple bar really did trade near $500.
- **Adjusted** is the split- and dividend-continuous series. Without it,
  a 4:1 split looks like a 75% crash, and every structural feature
  computed across it — decline depth, consolidation range, breakout
  magnitude — is nonsense.

Adjustment is recomputed here from the corporate actions ARGUS has
stored, rather than taken from FMP's `adjClose`. Two reasons: a vendor's
adjusted series can change silently between fetches, which breaks the
reproducibility guarantee that a recorded configuration re-run yields an
identical result; and an adjustment ARGUS computed can be explained from
the specific actions that produced it, which one it merely received
cannot.

Back-adjustment convention: the most recent bar keeps its raw price, and
earlier bars are scaled so the series is continuous through each action.
This is the standard convention and the one that keeps "today's price" a
real number.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from data.canonical_model.records import (
    CanonicalCorporateAction,
    CanonicalCorporateActionType,
    CanonicalOhlcvBar,
)
from data.normalization.translate import split_ratio

#: Rounding for adjusted prices. Module 03 stores Numeric(20, 6).
_PRICE_EXPONENT = Decimal("0.000001")


@dataclass(slots=True)
class AdjustmentReport:
    """Corporate actions that could not be applied, and why."""

    skipped: list[tuple[date, str]] = field(default_factory=list)

    def skip(self, effective: date, reason: str) -> None:
        self.skipped.append((effective, reason))

    def __bool__(self) -> bool:
        return not self.skipped


@dataclass(frozen=True, slots=True)
class _Factor:
    """A multiplicative adjustment taking effect on `effective_date`.

    Bars strictly *before* the effective date are scaled; the bar on the
    effective date already reflects the action.
    """

    effective_date: date
    price_multiplier: Decimal
    volume_multiplier: Decimal


def build_adjustment_factors(
    actions: list[CanonicalCorporateAction],
    bars_by_date: dict[date, CanonicalOhlcvBar],
    *,
    include_dividends: bool = True,
    report: AdjustmentReport | None = None,
) -> list[_Factor]:
    """Turn corporate actions into dated adjustment factors.

    Splits scale price by `denominator/numerator` and volume inversely: a
    4:1 split means one old share became four, so pre-split prices are
    quartered and pre-split volumes quadrupled to stay comparable.

    Dividends scale price by `(close_before_ex - dividend) /
    close_before_ex`, the standard total-return adjustment. It needs the
    close on the session before the ex-date; when that bar is absent
    (a gap, or the series starting after the dividend) the dividend is
    skipped and reported rather than approximated, since a wrong factor
    silently distorts every earlier bar.
    """
    report = report if report is not None else AdjustmentReport()
    ordered_dates = sorted(bars_by_date)
    factors: list[_Factor] = []

    for action in actions:
        if action.action_type is CanonicalCorporateActionType.SPLIT:
            ratio = split_ratio(action.details)
            if ratio is None:
                report.skip(action.effective_date, "split has no usable numerator/denominator")
                continue
            factors.append(
                _Factor(
                    effective_date=action.effective_date,
                    price_multiplier=Decimal(1) / ratio,
                    volume_multiplier=ratio,
                )
            )

        elif action.action_type is CanonicalCorporateActionType.DIVIDEND and include_dividends:
            amount = _dividend_amount(action.details)
            if amount is None or amount <= 0:
                report.skip(action.effective_date, "dividend has no usable amount")
                continue

            previous_close = _close_before(ordered_dates, bars_by_date, action.effective_date)
            if previous_close is None or previous_close <= 0:
                report.skip(action.effective_date, "no prior close to compute a dividend factor")
                continue
            if amount >= previous_close:
                report.skip(action.effective_date, "dividend exceeds the prior close")
                continue

            factors.append(
                _Factor(
                    effective_date=action.effective_date,
                    price_multiplier=(previous_close - amount) / previous_close,
                    volume_multiplier=Decimal(1),
                )
            )

    factors.sort(key=lambda factor: factor.effective_date)
    return factors


def apply_adjustments(
    bars: list[CanonicalOhlcvBar],
    actions: list[CanonicalCorporateAction],
    *,
    include_dividends: bool = True,
    report: AdjustmentReport | None = None,
) -> list[CanonicalOhlcvBar]:
    """Return the bars with their adjusted series populated.

    Raw values are never touched — new records are returned with the
    adjusted fields filled in, and the originals are left exactly as
    they were. A bar with no actions after it gets adjusted values equal
    to its raw ones rather than nulls, so downstream code can read the
    adjusted series uniformly without a null check per bar.
    """
    if not bars:
        return []

    bars_by_date = {bar.pit.event_time.date(): bar for bar in bars}
    factors = build_adjustment_factors(
        actions, bars_by_date, include_dividends=include_dividends, report=report
    )

    # Cumulative product of every factor taking effect strictly after a
    # bar, walking backwards so each bar sees only later actions.
    adjusted: list[CanonicalOhlcvBar] = []
    ordered = sorted(bars, key=lambda bar: bar.pit.event_time)

    cumulative_price = Decimal(1)
    cumulative_volume = Decimal(1)
    factor_index = len(factors) - 1

    for bar in reversed(ordered):
        bar_date = bar.pit.event_time.date()
        while factor_index >= 0 and factors[factor_index].effective_date > bar_date:
            cumulative_price *= factors[factor_index].price_multiplier
            cumulative_volume *= factors[factor_index].volume_multiplier
            factor_index -= 1

        adjusted.append(
            bar.model_copy(
                update={
                    "open_adjusted": _round(bar.open_raw * cumulative_price),
                    "high_adjusted": _round(bar.high_raw * cumulative_price),
                    "low_adjusted": _round(bar.low_raw * cumulative_price),
                    "close_adjusted": _round(bar.close_raw * cumulative_price),
                    "volume_adjusted": int(Decimal(bar.volume_raw) * cumulative_volume),
                }
            )
        )

    adjusted.reverse()
    return adjusted


def _dividend_amount(details: dict) -> Decimal | None:
    # `adjDividend` is FMP's split-adjusted figure; `dividend` is the
    # cash actually paid. The raw cash amount is correct here because it
    # is being applied against the raw close of the same era.
    for key in ("dividend", "adjDividend", "amount"):
        value = details.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError):
            continue
    return None


def _close_before(
    ordered_dates: list[date],
    bars_by_date: dict[date, CanonicalOhlcvBar],
    target: date,
) -> Decimal | None:
    """Raw close on the last session strictly before `target`."""
    position = bisect_left(ordered_dates, target)
    if position == 0:
        return None
    return bars_by_date[ordered_dates[position - 1]].close_raw


def _round(value: Decimal) -> Decimal:
    return value.quantize(_PRICE_EXPONENT)
