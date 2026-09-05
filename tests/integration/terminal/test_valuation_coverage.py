"""Which fields `/valuation` actually returns, against a realistic payload.

This file exists to answer a question rather than to guard a behaviour:
*if ARGUS had a live Ultimate key, which of the figures a reader expects
from a stock-data site would appear on the Terminal's valuation
endpoint, and which would not?*

`read_valuation` merges the whole stored payload of KEY_METRICS, RATIOS
and FINANCIAL_SCORES without filtering, so the answer is entirely
determined by what FMP puts in those three responses. The fixtures below
are the documented field names for those endpoints. No live key was
available to confirm them — the same caveat every FMP field name in this
codebase carries — so what this proves is the *merge*, not FMP's
spelling: whatever those endpoints return reaches the response under the
provider's own key, and `sources` says which of the three it came from.

## What is not here, and why

Two figures a reader will look for are absent from every one of the three
payloads, and no arrangement of this code produces them:

- **Short interest.** FMP has no endpoint for it on any plan. There is
  nothing to store and nothing to show, so no field exists — an
  always-null one would look like a gap ARGUS could close.
- **Insider ownership percentage.** FMP reports insider transactions,
  not a held percentage.

**Institutional ownership percentage** is also absent from these three,
but for a different reason: it lives on a different endpoint. It is
served by `/ownership` from the 13F summaries Module 26 ingests, as the
provider's own figure — see `test_ultimate_data.py` and
`services/terminal/related.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from data.canonical_model.records import CanonicalStatementType
from tests.integration.terminal.conftest import NOW

PERIOD_END = datetime(2025, 12, 31, tzinfo=UTC)
FILED = datetime(2026, 2, 20, tzinfo=UTC)

#: FMP `/stable/key-metrics`, documented fields. Per-share and absolute
#: figures, plus the returns and the enterprise-value multiples.
KEY_METRICS = {
    "marketCap": 3_100_000_000_000,
    "enterpriseValue": 3_050_000_000_000,
    "evToSales": 8.9,
    "evToEBITDA": 22.4,
    "evToOperatingCashFlow": 24.1,
    "evToFreeCashFlow": 27.3,
    "returnOnEquity": 1.47,
    "returnOnAssets": 0.28,
    "returnOnInvestedCapital": 0.56,
    "returnOnCapitalEmployed": 0.61,
    "freeCashFlowYield": 0.036,
    "earningsYield": 0.028,
    "workingCapital": -2_000_000_000,
    "netDebtToEBITDA": 0.4,
    "currentRatio": 0.94,
    "researchAndDevelopementToRevenue": 0.079,
    "grahamNumber": 25.4,
    "weightedAverageShares": 15_400_000_000,
    "tangibleAssetValue": 62_000_000_000,
    # WACC — present on FMP's key-metrics, and one of the figures that
    # would otherwise look like an obvious candidate for ARGUS to compute.
    "weightedAverageCostOfCapital": 0.088,
}

#: FMP `/stable/ratios`, documented fields. Margins, turnover, coverage —
#: the per-share and per-period ratios rather than the valuation levels.
RATIOS = {
    "grossProfitMargin": 0.461,
    "operatingProfitMargin": 0.318,
    "netProfitMargin": 0.264,
    "ebitdaMargin": 0.343,
    "priceToEarningsRatio": 35.7,
    "priceToBookRatio": 51.2,
    "priceToSalesRatio": 9.1,
    "priceToFreeCashFlowRatio": 28.0,
    "debtToEquityRatio": 1.87,
    "quickRatio": 0.90,
    "cashRatio": 0.21,
    "interestCoverageRatio": 45.0,
    "assetTurnover": 1.07,
    "inventoryTurnover": 33.5,
    "receivablesTurnover": 12.4,
    "payablesTurnover": 3.2,
    "dividendYield": 0.0045,
    "dividendPayoutRatio": 0.157,
    "effectiveTaxRate": 0.241,
}

#: FMP `/stable/financial-scores`. The two composite scores, on their own
#: endpoint — neither appears in key-metrics or ratios, which is why this
#: was added as a third `VALUATION_TYPES` member rather than assumed.
FINANCIAL_SCORES = {
    "altmanZScore": 9.12,
    "piotroskiScore": 8,
    "workingCapital": -1_900_000_000,
    "totalAssets": 364_000_000_000,
    "retainedEarnings": -19_000_000_000,
    "ebit": 123_000_000_000,
    "marketCap": 3_100_000_000_000,
    "revenue": 391_000_000_000,
}


def _stock_with_all_three(register, add_fundamentals, ticker: str):
    security_id = register(ticker)
    for statement_type, data in (
        (CanonicalStatementType.KEY_METRICS, KEY_METRICS),
        (CanonicalStatementType.RATIOS, RATIOS),
        (CanonicalStatementType.FINANCIAL_SCORES, FINANCIAL_SCORES),
    ):
        add_fundamentals(
            security_id,
            statement_type=statement_type,
            fiscal_period="FY2025",
            fiscal_period_end=PERIOD_END,
            available_at=FILED,
            data=data,
        )
    return security_id


def test_every_stored_valuation_field_reaches_the_response(client, register, add_fundamentals):
    """The merge is unfiltered, so the answer is "all of them".

    Asserted as a set equality rather than a spot check: a filter added
    later — an allow-list, a rename, a "tidy up the keys" pass — would
    fail here rather than silently shrinking what the Terminal shows.
    """
    _stock_with_all_three(register, add_fundamentals, "VALA")

    body = client.get(
        "/terminal/companies/VALA/valuation", params={"as_of": NOW.isoformat()}
    ).json()

    expected = set(KEY_METRICS) | set(RATIOS) | set(FINANCIAL_SCORES)
    assert set(body["metrics"]) == expected
    assert body["unavailable"] == {}


def test_each_field_says_which_statement_it_came_from(client, register, add_fundamentals):
    """`sources` is what makes an unexpected number traceable.

    `marketCap` and `workingCapital` appear in two of the three payloads
    with different values, and the first statement wins. Which one that
    was has to be visible, or a reader comparing ARGUS to another site
    has no way to find out why the numbers differ.
    """
    _stock_with_all_three(register, add_fundamentals, "VALB")

    body = client.get(
        "/terminal/companies/VALB/valuation", params={"as_of": NOW.isoformat()}
    ).json()

    assert body["sources"]["returnOnEquity"] == "KEY_METRICS"
    assert body["sources"]["grossProfitMargin"] == "RATIOS"
    assert body["sources"]["altmanZScore"] == "FINANCIAL_SCORES"
    assert body["sources"]["piotroskiScore"] == "FINANCIAL_SCORES"
    # Collision: KEY_METRICS is first in VALUATION_TYPES, so its figure
    # is the one shown and `sources` says so.
    assert body["sources"]["marketCap"] == "KEY_METRICS"
    assert body["metrics"]["workingCapital"] == KEY_METRICS["workingCapital"]
    assert body["sources"]["workingCapital"] == "KEY_METRICS"


def test_financial_scores_are_reported_missing_when_only_the_other_two_exist(
    client, register, add_fundamentals
):
    """The third type is a real dependency, not a bonus.

    Altman Z and Piotroski F appear nowhere in key-metrics or ratios, so
    a security without the scores endpoint has them named as unavailable
    rather than quietly absent from `metrics`.
    """
    security_id = register("VALC")
    for statement_type, data in (
        (CanonicalStatementType.KEY_METRICS, KEY_METRICS),
        (CanonicalStatementType.RATIOS, RATIOS),
    ):
        add_fundamentals(
            security_id,
            statement_type=statement_type,
            fiscal_period="FY2025",
            fiscal_period_end=PERIOD_END,
            available_at=FILED,
            data=data,
        )

    body = client.get(
        "/terminal/companies/VALC/valuation", params={"as_of": NOW.isoformat()}
    ).json()

    assert "altmanZScore" not in body["metrics"]
    assert "FINANCIAL_SCORES" in body["unavailable"]
    assert body["unavailable"]["FINANCIAL_SCORES"]["available"] is False
    # The other two still came through — one missing type does not empty
    # the panel.
    assert body["metrics"]["priceToEarningsRatio"] == 35.7


def test_no_valuation_field_is_named_for_short_interest(client, register, add_fundamentals):
    """The absence, asserted so it stays deliberate.

    FMP has no short-interest endpoint on any plan. If a future payload
    ever carries one, this fails and the decision gets made on purpose
    rather than by a field quietly appearing.
    """
    _stock_with_all_three(register, add_fundamentals, "VALD")

    body = client.get(
        "/terminal/companies/VALD/valuation", params={"as_of": NOW.isoformat()}
    ).json()

    assert not [key for key in body["metrics"] if "short" in key.lower()]


def test_valuation_still_honours_the_cutoff(client, register, add_fundamentals):
    """Merged, but never ahead of time.

    The merge is the only thing this endpoint does beyond a PIT read, and
    it must not become a way around one: a February filing is invisible
    to a January query.
    """
    security_id = register("VALE")
    add_fundamentals(
        security_id,
        statement_type=CanonicalStatementType.KEY_METRICS,
        fiscal_period="FY2025",
        fiscal_period_end=PERIOD_END,
        available_at=FILED,
        data=KEY_METRICS,
    )

    body = client.get(
        "/terminal/companies/VALE/valuation",
        params={"as_of": datetime(2026, 1, 15, tzinfo=UTC).isoformat()},
    ).json()

    assert body["metrics"] == {}
    assert set(body["unavailable"]) == {"KEY_METRICS", "RATIOS", "FINANCIAL_SCORES"}
