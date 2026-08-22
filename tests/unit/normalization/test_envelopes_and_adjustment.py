"""Envelope absorption, corporate action adjustment, and validation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from data.canonical_model.exchanges import CanonicalExchange, normalize_exchange, normalize_symbol
from data.canonical_model.pit import PitTimestamps, session_close
from data.canonical_model.records import (
    CanonicalCorporateAction,
    CanonicalCorporateActionType,
    CanonicalOhlcvBar,
    SourceLineage,
)
from data.normalization.adjustments import AdjustmentReport, apply_adjustments
from data.normalization.envelopes import unwrap_envelope
from data.normalization.validation import ValidationIssue, validate_bars

SECURITY_ID = uuid4()
LINEAGE = SourceLineage(provider="fmp", endpoint="historical_price_eod_full")


# --------------------------------------------------------------------------
# Envelope shapes
# --------------------------------------------------------------------------


def test_bare_array_envelope():
    """Shape 1: FMP's most common response."""
    assert unwrap_envelope([{"symbol": "AAPL"}, {"symbol": "MSFT"}]) == [
        {"symbol": "AAPL"},
        {"symbol": "MSFT"},
    ]


def test_keyed_wrapper_envelope():
    """Shape 2: the payload nested under a key."""
    assert unwrap_envelope({"historical": [{"date": "2024-01-03"}]}) == [{"date": "2024-01-03"}]


def test_bare_object_envelope():
    """Shape 3: a single record delivered with no wrapper."""
    assert unwrap_envelope({"symbol": "AAPL", "price": 189.5}) == [
        {"symbol": "AAPL", "price": 189.5}
    ]


@pytest.mark.parametrize("key", ["historical", "data", "results", "items"])
def test_every_known_wrapper_key_is_absorbed(key):
    assert unwrap_envelope({key: [{"a": 1}]}) == [{"a": 1}]


def test_empty_and_unreadable_payloads_yield_nothing():
    assert unwrap_envelope(None) == []
    assert unwrap_envelope([]) == []
    assert unwrap_envelope({}) == []
    assert unwrap_envelope("not a payload") == []


def test_non_dict_entries_are_discarded():
    assert unwrap_envelope([{"a": 1}, "junk", None, {"b": 2}]) == [{"a": 1}, {"b": 2}]


# --------------------------------------------------------------------------
# Exchange and symbol normalization
# --------------------------------------------------------------------------


def test_long_exchange_name_wins_over_inconsistent_short_name():
    """FMP labels NYSE Arca ETFs with exchangeShortName="AMEX"."""
    assert normalize_exchange("NYSE Arca", "AMEX") is CanonicalExchange.NYSE_ARCA


def test_nasdaq_variants_all_resolve_to_nasdaq():
    for label in ("NASDAQ Global Select", "NASDAQ Capital Market", "NASDAQ"):
        assert normalize_exchange(label, None) is CanonicalExchange.NASDAQ


def test_new_york_stock_exchange_resolves_to_nyse():
    assert normalize_exchange("New York Stock Exchange", "NYSE") is CanonicalExchange.NYSE


def test_short_name_is_used_when_long_name_is_absent():
    assert normalize_exchange(None, "NASDAQ") is CanonicalExchange.NASDAQ


def test_unrecognised_exchange_becomes_unknown_never_a_guess():
    """A wrong guess would put securities into Module 06's universe silently."""
    assert normalize_exchange("Bucharest Stock Exchange", "BVB") is CanonicalExchange.UNKNOWN


def test_symbol_suffixes_are_preserved():
    """Stripping .TO would collapse a Toronto listing onto its US namesake."""
    assert normalize_symbol(" shop.to ") == "SHOP.TO"
    assert normalize_symbol("aapl") == "AAPL"


# --------------------------------------------------------------------------
# Adjustment
# --------------------------------------------------------------------------


def _bar(day: date, close: str, volume: int = 1000) -> CanonicalOhlcvBar:
    close_time = session_close(day)
    price = Decimal(close)
    return CanonicalOhlcvBar(
        security_id=SECURITY_ID,
        pit=PitTimestamps.derive(
            event_time=close_time,
            observation_time=close_time,
            ingestion_time=datetime(2026, 1, 1, tzinfo=UTC),
            lag=timedelta(hours=16),
        ),
        lineage=LINEAGE,
        open_raw=price,
        high_raw=price,
        low_raw=price,
        close_raw=price,
        volume_raw=volume,
    )


def _split(day: date, numerator: int, denominator: int = 1) -> CanonicalCorporateAction:
    return CanonicalCorporateAction(
        security_id=SECURITY_ID,
        action_type=CanonicalCorporateActionType.SPLIT,
        effective_date=day,
        pit=PitTimestamps.derive(
            event_time=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            observation_time=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            ingestion_time=datetime(2026, 1, 1, tzinfo=UTC),
            lag=timedelta(hours=24),
        ),
        lineage=SourceLineage(provider="fmp", endpoint="splits"),
        details={"numerator": numerator, "denominator": denominator},
    )


def _dividend(day: date, amount: str) -> CanonicalCorporateAction:
    return CanonicalCorporateAction(
        security_id=SECURITY_ID,
        action_type=CanonicalCorporateActionType.DIVIDEND,
        effective_date=day,
        pit=PitTimestamps.derive(
            event_time=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            observation_time=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            ingestion_time=datetime(2026, 1, 1, tzinfo=UTC),
            lag=timedelta(hours=24),
        ),
        lineage=SourceLineage(provider="fmp", endpoint="dividends"),
        details={"dividend": amount},
    )


def test_split_adjustment_keeps_the_raw_series_untouched():
    """Raw is point-in-time reality: the stock really did trade at 400."""
    bars = [_bar(date(2020, 8, 28), "400"), _bar(date(2020, 8, 31), "100")]
    adjusted = apply_adjustments(bars, [_split(date(2020, 8, 31), 4)])

    assert adjusted[0].close_raw == Decimal("400")
    assert adjusted[1].close_raw == Decimal("100")


def test_split_adjustment_makes_the_series_continuous():
    """Without it, a 4:1 split looks like a 75% crash to every feature."""
    bars = [_bar(date(2020, 8, 28), "400"), _bar(date(2020, 8, 31), "100")]
    adjusted = apply_adjustments(bars, [_split(date(2020, 8, 31), 4)])

    assert adjusted[0].close_adjusted == Decimal("100.000000")
    assert adjusted[1].close_adjusted == Decimal("100.000000")


def test_split_adjustment_scales_volume_inversely():
    bars = [
        _bar(date(2020, 8, 28), "400", volume=1000),
        _bar(date(2020, 8, 31), "100", volume=4000),
    ]
    adjusted = apply_adjustments(bars, [_split(date(2020, 8, 31), 4)])

    assert adjusted[0].volume_adjusted == 4000
    assert adjusted[1].volume_adjusted == 4000


def test_bar_on_the_effective_date_is_not_adjusted():
    """It already reflects the action."""
    bars = [_bar(date(2020, 8, 31), "100")]
    adjusted = apply_adjustments(bars, [_split(date(2020, 8, 31), 4)])

    assert adjusted[0].close_adjusted == Decimal("100.000000")


def test_multiple_splits_compound():
    bars = [
        _bar(date(2014, 6, 6), "700"),
        _bar(date(2020, 8, 28), "100"),
        _bar(date(2020, 9, 1), "25"),
    ]
    adjusted = apply_adjustments(bars, [_split(date(2014, 6, 9), 7), _split(date(2020, 8, 31), 4)])

    # 700 / 7 / 4 = 25
    assert adjusted[0].close_adjusted == Decimal("25.000000")
    assert adjusted[1].close_adjusted == Decimal("25.000000")


def test_bars_with_no_later_action_get_adjusted_equal_to_raw():
    """Uniform reads downstream, rather than a null check per bar."""
    bars = [_bar(date(2024, 1, 3), "184.25")]
    adjusted = apply_adjustments(bars, [])

    assert adjusted[0].close_adjusted == Decimal("184.250000")
    assert adjusted[0].volume_adjusted == adjusted[0].volume_raw


def test_dividend_adjustment_uses_the_prior_close():
    bars = [_bar(date(2024, 2, 8), "100"), _bar(date(2024, 2, 9), "99.76")]
    adjusted = apply_adjustments(bars, [_dividend(date(2024, 2, 9), "1.00")])

    # (100 - 1) / 100 = 0.99
    assert adjusted[0].close_adjusted == Decimal("99.000000")
    assert adjusted[1].close_adjusted == Decimal("99.760000")


def test_dividend_without_a_prior_close_is_skipped_and_reported():
    """A guessed factor would silently distort every earlier bar."""
    bars = [_bar(date(2024, 2, 9), "99.76")]
    report = AdjustmentReport()
    adjusted = apply_adjustments(bars, [_dividend(date(2024, 2, 9), "1.00")], report=report)

    assert adjusted[0].close_adjusted == Decimal("99.760000")
    assert len(report.skipped) == 1
    assert "prior close" in report.skipped[0][1]


def test_split_with_an_unusable_ratio_is_skipped_and_reported():
    action = _split(date(2020, 8, 31), 4)
    broken = action.model_copy(update={"details": {}})
    report = AdjustmentReport()

    apply_adjustments([_bar(date(2020, 8, 28), "400")], [broken], report=report)

    assert len(report.skipped) == 1


def test_dividend_adjustment_can_be_disabled():
    bars = [_bar(date(2024, 2, 8), "100"), _bar(date(2024, 2, 9), "99.76")]
    adjusted = apply_adjustments(
        bars, [_dividend(date(2024, 2, 9), "1.00")], include_dividends=False
    )

    assert adjusted[0].close_adjusted == Decimal("100.000000")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_negative_volume_is_fatal():
    bar = _bar(date(2024, 1, 3), "100").model_copy(update={"volume_raw": -5})
    result = validate_bars([bar])

    assert result.accepted == []
    assert ValidationIssue.NEGATIVE_VOLUME in result.flagged[0].issues


def test_high_below_low_is_fatal():
    bar = _bar(date(2024, 1, 3), "100").model_copy(
        update={"high_raw": Decimal("90"), "low_raw": Decimal("110")}
    )
    result = validate_bars([bar])

    assert result.accepted == []
    assert ValidationIssue.HIGH_BELOW_LOW in result.flagged[0].issues


def test_non_positive_price_is_fatal():
    bar = _bar(date(2024, 1, 3), "100").model_copy(update={"low_raw": Decimal("0")})
    result = validate_bars([bar])

    assert result.accepted == []


def test_large_move_without_a_corporate_action_is_flagged_but_still_accepted():
    """ARGUS exists to find extreme moves — flagging must not mean discarding."""
    bars = [_bar(date(2024, 1, 3), "100"), _bar(date(2024, 1, 4), "160")]
    result = validate_bars(bars)

    assert len(result.accepted) == 2
    assert len(result.advisory) == 1
    assert ValidationIssue.UNEXPLAINED_PRICE_JUMP in result.advisory[0].issues
    assert result.fatal == []


def test_a_matching_corporate_action_explains_the_move():
    bars = [_bar(date(2020, 8, 28), "400"), _bar(date(2020, 8, 31), "100")]
    result = validate_bars(bars, [_split(date(2020, 8, 31), 4)])

    assert result.flagged == []
    assert len(result.accepted) == 2


def test_clean_bars_pass():
    bars = [_bar(date(2024, 1, 3), "100"), _bar(date(2024, 1, 4), "101")]
    result = validate_bars(bars)

    assert len(result.accepted) == 2
    assert bool(result) is True
