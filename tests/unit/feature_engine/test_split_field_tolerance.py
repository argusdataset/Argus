"""Reading a split's ratio: alias tolerance, and never a silent skip.

Splits were the last FMP field group in the codebase read from fixed key
names. Every other one goes through an alias table, because the field
names came from documentation rather than from a live key — and splits
carried the same risk with worse consequences. An unresolved analyst
grade is a missing panel; an unresolved split is a *wrong price series*,
and Module 15 records the resulting −50% single bar as a catastrophic
failure of a setup that actually succeeded. That is issue G2's own
failure mode, and without the tolerance it could return with no error
message at all.

Two properties are under test, and the second is the one that would have
made the difference:

1. **Tolerance.** Several plausible spellings resolve, including a single
   combined `splitRatio`.
2. **Visibility.** A ratio that cannot be read is logged and counted.
   `data/normalization/adjustments.py` always reported this case through
   `report.skip`; `core/feature_engine/panel.py` did not, so one rule had
   two implementations that disagreed about whether anyone should be
   told. There is now one implementation of the ratio and both callers
   report.

The alias list is still a guess. That is precisely why the reporting
matters more than the tolerance: an incomplete alias list plus a visible
counter is a problem found on day one, while a complete-looking alias
list and silence is a problem found in a backtest months later.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pandas as pd
import pytest

from core.feature_engine.panel import build_adjustment_factors
from data.canonical_model.records import CanonicalCorporateActionType
from data.normalization.translate import SPLIT_FIELD_ALIASES, split_ratio

SECURITY = uuid4()
DATES = pd.date_range("2026-02-10", periods=6, freq="B", tz="UTC")
EFFECTIVE = datetime(2026, 2, 13, tzinfo=UTC)


def _closes() -> pd.DataFrame:
    return pd.DataFrame(100.0, index=DATES, columns=[SECURITY])


def _actions(details: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "security_id": SECURITY,
                "action_type": CanonicalCorporateActionType.SPLIT.value,
                "effective_date": EFFECTIVE,
                "details": details,
            }
        ]
    )


# --------------------------------------------------------------------------
# Tolerance
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "details",
    [
        {"numerator": 2, "denominator": 1},
        {"splitNumerator": 2, "splitDenominator": 1},
        {"newShares": 2, "oldShares": 1},
        {"toFactor": 2, "fromFactor": 1},
        # A single combined figure, which FMP is documented as returning
        # on some endpoints. Read as new-per-old.
        {"splitRatio": 2},
        {"ratio": "2.0"},
        # Strings, because a JSON payload is entitled to send numbers as
        # strings and several FMP endpoints do.
        {"numerator": "2", "denominator": "1"},
    ],
)
def test_a_two_for_one_split_resolves_under_every_accepted_spelling(details: dict[str, Any]):
    assert split_ratio(details) == Decimal(2)


def test_the_pair_wins_over_a_combined_ratio_when_both_are_present():
    """Unambiguous beats ambiguous.

    A payload carrying both is a payload whose author disagreed with
    itself; the explicit pair says new-per-old without relying on a
    convention.
    """
    assert split_ratio({"numerator": 3, "denominator": 2, "splitRatio": 99}) == Decimal("1.5")


@pytest.mark.parametrize(
    "details",
    [
        {},
        {"numerator": 2},  # half a pair and no combined figure
        {"denominator": 1},
        {"numerator": 0, "denominator": 1},  # a zero ratio is not a split
        {"numerator": -2, "denominator": 1},
        {"numerator": "two", "denominator": "one"},
        {"splitRatio": ""},
    ],
)
def test_an_unusable_payload_resolves_to_nothing_rather_than_to_one(details: dict[str, Any]):
    """`None`, never a default of 1.0.

    A ratio of 1 is a real answer meaning "no adjustment", and returning
    it for an unreadable payload would make an unadjusted series
    indistinguishable from a correctly-adjusted one — which is exactly
    the confusion that has to stay impossible here.
    """
    assert split_ratio(details) is None


def test_every_alias_the_table_lists_is_actually_tried():
    """The table is documentation as well as behaviour.

    An alias listed and not consulted would tell the next reader a
    spelling is covered when it is not — the kind of drift that makes a
    tolerance table worse than none.
    """
    for numerator, denominator in zip(
        SPLIT_FIELD_ALIASES["numerator"], SPLIT_FIELD_ALIASES["denominator"], strict=True
    ):
        assert split_ratio({numerator: 2, denominator: 1}) == Decimal(2)

    for combined in SPLIT_FIELD_ALIASES["ratio"]:
        assert split_ratio({combined: 4}) == Decimal(4)


# --------------------------------------------------------------------------
# Visibility
# --------------------------------------------------------------------------


def test_a_resolvable_split_adjusts_the_earlier_bars():
    """The baseline the next test is measured against."""
    factors = build_adjustment_factors(_actions({"numerator": 2, "denominator": 1}), _closes())

    before = factors.loc[factors.index < EFFECTIVE, SECURITY]
    after = factors.loc[factors.index >= EFFECTIVE, SECURITY]

    assert (before == 0.5).all()
    assert (after == 1.0).all()


def test_an_unreadable_split_leaves_the_series_unadjusted_and_says_so(caplog):
    """The silence that let G2's failure mode return unannounced.

    The series is still unadjusted — there is no honest factor to apply
    when the ratio cannot be read. What changes is that the run now says
    how many splits it could not read, and which spellings it tried, so a
    provider rename is visible on its first day instead of surfacing
    months later as an inexplicable −50% bar in an outcome record.
    """
    with caplog.at_level(logging.WARNING, logger="argus.feature_engine.panel"):
        factors = build_adjustment_factors(_actions({"ratioNobodyExpected": 2}), _closes())

    assert (factors[SECURITY] == 1.0).all()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].event == "split_ratio_unresolved"
    assert warnings[0].unresolved_splits == 1
    assert warnings[0].securities == 1
    # The tried spellings ride along, so the fix is a one-line change to
    # the alias table rather than an investigation.
    assert "numerator" in warnings[0].tried
    assert "splitRatio" in warnings[0].tried


def test_a_readable_split_logs_nothing(caplog):
    """A warning per healthy run would train everyone to ignore it."""
    with caplog.at_level(logging.WARNING, logger="argus.feature_engine.panel"):
        build_adjustment_factors(_actions({"numerator": 2, "denominator": 1}), _closes())

    assert [record for record in caplog.records if record.levelno == logging.WARNING] == []


def test_one_unreadable_split_does_not_cost_a_readable_one_its_adjustment():
    """Per-split, not per-run.

    A security whose split reads correctly must still be adjusted when
    another security's does not — otherwise one bad payload would
    silently unadjust the whole universe.
    """
    other = uuid4()
    closes = pd.DataFrame(100.0, index=DATES, columns=[SECURITY, other])
    actions = pd.DataFrame(
        [
            {
                "security_id": SECURITY,
                "action_type": CanonicalCorporateActionType.SPLIT.value,
                "effective_date": EFFECTIVE,
                "details": {"nothingUsable": True},
            },
            {
                "security_id": other,
                "action_type": CanonicalCorporateActionType.SPLIT.value,
                "effective_date": EFFECTIVE,
                "details": {"numerator": 2, "denominator": 1},
            },
        ]
    )

    factors = build_adjustment_factors(actions, closes)

    assert (factors[SECURITY] == 1.0).all()
    assert (factors.loc[factors.index < EFFECTIVE, other] == 0.5).all()
