"""The nine new routes over HTTP, envelope included.

`test_ultimate_data.py` tests the reads. This file tests the *contract* —
that each route exists at the path a client will call, serialises the
schema it declares, and renders an absence as a JSON object with a reason
rather than as `null`.

That last one is worth a separate layer. A read function returning
`unavailable=None` and a route returning `"unavailable": null` look
identical in Python and mean different things to a consumer that only
ever sees JSON, which is the audience `schemas.py` opens by naming.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from data.canonical_model.records import CanonicalDisclosureType, CanonicalSnapshotType
from tests.integration.terminal.conftest import NOW

CUTOFF = NOW.isoformat()
LAST_WEEK = NOW - timedelta(days=7)


def test_every_new_route_answers_for_a_known_security(client, register):
    """All nine paths exist and return 200 with nothing ingested.

    An empty ARGUS is the honest current state, and these endpoints must
    answer rather than 404 — a security that resolves but has no data is
    a 200 with a named absence, which `errors.py` states as the rule and
    this asserts for every one of the new routes at once.
    """
    register("HTTPA")
    paths = (
        "analyst-estimates",
        "price-target",
        "grades",
        "executive-compensation",
        "transcripts",
        "peers",
        "holdings",
        "indicators/rsi",
        "indicators/sma",
    )

    for path in paths:
        response = client.get(f"/terminal/companies/HTTPA/{path}", params={"as_of": CUTOFF})
        assert response.status_code == 200, path
        body = response.json()
        assert body["security"]["ticker"] == "HTTPA", path
        # The absence is an object with a reason, never a null.
        assert body["unavailable"] is not None, path
        assert body["unavailable"]["available"] is False, path
        assert body["unavailable"]["reason"], path


def test_an_unknown_ticker_is_still_a_404(client):
    """Absence of data and absence of a security stay distinguishable."""
    response = client.get("/terminal/companies/ZZZZ/peers")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SECURITY_NOT_FOUND"


def test_estimates_serialise_the_provider_payload_unrenamed(client, register, add_disclosure):
    """`data` passes through with the provider's own field names.

    Renaming here would create a second vocabulary for the same numbers,
    which `FinancialStatement` refuses for statements and this refuses
    for the same reason.
    """
    security_id = register("HTTPB")
    add_disclosure(
        security_id,
        disclosure_type=CanonicalDisclosureType.ANALYST_ESTIMATES,
        fiscal_period="2027",
        available_at=LAST_WEEK,
        data={"estimatedRevenueAvg": 1_200_000, "numberAnalystEstimatedRevenue": 12},
    )

    body = client.get(
        "/terminal/companies/HTTPB/analyst-estimates", params={"as_of": CUTOFF}
    ).json()

    assert body["unavailable"] is None
    assert body["periods"][0]["data"]["numberAnalystEstimatedRevenue"] == 12
    assert body["as_of"].startswith("2026-03-03")


def test_grades_respect_the_limit_parameter(client, register, add_grade):
    security_id = register("HTTPC")
    for day in range(1, 6):
        add_grade(
            security_id,
            grading_company=f"Firm {day}",
            graded_at=NOW - timedelta(days=day),
        )

    body = client.get(
        "/terminal/companies/HTTPC/grades", params={"as_of": CUTOFF, "limit": 2}
    ).json()

    assert len(body["grades"]) == 2
    assert body["grades"][0]["grading_company"] == "Firm 1"


def test_holdings_return_the_disclosed_position_count(client, register, add_snapshot):
    security_id = register("HTTPD")
    add_snapshot(
        security_id,
        snapshot_type=CanonicalSnapshotType.FUND_HOLDINGS,
        available_at=LAST_WEEK,
        data={
            "source": "fund_disclosure",
            "position_count": 42,
            "holdings": [{"asset": "AAA"}, {"asset": "BBB"}],
        },
    )

    body = client.get("/terminal/companies/HTTPD/holdings", params={"as_of": CUTOFF}).json()

    assert body["position_count"] == 42
    assert len(body["holdings"]) == 2
    assert body["source"] == "fund_disclosure"


def test_indicator_route_echoes_the_series_parameters(client, register, add_indicator):
    """A chart has to be able to see which series it was given.

    `indicator`, `period_length` and `timeframe` come back on the
    response because they are part of what the numbers mean — a client
    that requested a 14-period RSI and rendered a 50-period one would
    have no way to notice.
    """
    security_id = register("HTTPE")
    add_indicator(
        security_id,
        indicator="rsi",
        period_length=14,
        bar_time=NOW - timedelta(days=1),
        value=Decimal("62.5"),
    )

    body = client.get("/terminal/companies/HTTPE/indicators/rsi", params={"as_of": CUTOFF}).json()

    assert body["indicator"] == "rsi"
    assert body["period_length"] == 14
    assert body["timeframe"] == "1day"
    assert body["points"][0]["value"] == 62.5


def test_an_unknown_indicator_is_a_400_naming_the_allowed_set(client, register):
    """A client can populate its selector from the rejection itself."""
    register("HTTPF")

    response = client.get("/terminal/companies/HTTPF/indicators/ichimoku")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "UNKNOWN_INDICATOR"
    assert "adx" in error["detail"]["allowed"]
