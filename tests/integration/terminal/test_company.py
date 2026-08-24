"""Fundamentals, valuation and news over HTTP, against real PIT storage.

The interesting assertions here are all about *cutoffs* and *absence*.
Anyone can return a row; the two things this module has to get right are
never returning one that was not knowable yet, and saying clearly when
there is nothing rather than returning a null a consumer has to guess at.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from data.canonical_model.records import CanonicalStatementType
from tests.integration.terminal.conftest import NOW

MARCH_QUARTER_END = datetime(2025, 3, 31, tzinfo=UTC)
FILED_IN_MAY = datetime(2025, 5, 15, tzinfo=UTC)


def test_fundamentals_return_the_latest_statement_of_each_type(client, register, add_fundamentals):
    security_id = register("MLSS")
    add_fundamentals(
        security_id,
        statement_type=CanonicalStatementType.INCOME_STATEMENT,
        fiscal_period="Q1-2025",
        fiscal_period_end=MARCH_QUARTER_END,
        available_at=FILED_IN_MAY,
        data={"revenue": 1_000_000, "net_income": 90_000},
    )

    response = client.get("/terminal/companies/MLSS/fundamentals", params={"as_of": NOW})
    payload = response.json()

    assert response.status_code == 200
    assert payload["security"]["ticker"] == "MLSS"
    assert payload["statements"]["INCOME_STATEMENT"]["data"]["revenue"] == 1_000_000
    assert payload["statements"]["INCOME_STATEMENT"]["fiscal_period"] == "Q1-2025"


def test_a_statement_type_with_nothing_stored_is_explained_not_omitted(
    client, register, add_fundamentals
):
    """The contract's most important property. A consumer must be able to
    tell "not filed" from "we do not carry this" without guessing."""
    security_id = register("SLS")
    add_fundamentals(
        security_id,
        statement_type=CanonicalStatementType.INCOME_STATEMENT,
        fiscal_period="Q1-2025",
        fiscal_period_end=MARCH_QUARTER_END,
        available_at=FILED_IN_MAY,
        data={"revenue": 1},
    )

    payload = client.get("/terminal/companies/SLS/fundamentals", params={"as_of": NOW}).json()

    assert set(payload["statements"]) == {"INCOME_STATEMENT"}
    assert set(payload["unavailable"]) == {"BALANCE_SHEET", "CASH_FLOW"}
    for entry in payload["unavailable"].values():
        assert entry["available"] is False
        assert entry["reason"]
        assert entry["explanation"]


def test_a_statement_filed_after_the_cutoff_is_invisible(client, register, add_fundamentals):
    """The whole point of routing through Module 07 rather than querying
    `canonical_fundamentals` directly."""
    security_id = register("HIVE")
    add_fundamentals(
        security_id,
        statement_type=CanonicalStatementType.INCOME_STATEMENT,
        fiscal_period="Q1-2025",
        fiscal_period_end=MARCH_QUARTER_END,
        available_at=FILED_IN_MAY,
        data={"revenue": 5},
    )

    before = client.get(
        "/terminal/companies/HIVE/fundamentals",
        params={"as_of": FILED_IN_MAY - timedelta(days=1)},
    ).json()
    after = client.get(
        "/terminal/companies/HIVE/fundamentals",
        params={"as_of": FILED_IN_MAY + timedelta(days=1)},
    ).json()

    assert "INCOME_STATEMENT" in before["unavailable"]
    assert "INCOME_STATEMENT" in after["statements"]


def test_a_restatement_wins_only_once_it_was_knowable(client, register, add_fundamentals):
    """The correction `get_latest_fundamental_as_of` encodes, exercised
    end to end: a revised figure must not appear before it was filed."""
    security_id = register("ALX")
    for available_at, revenue in (
        (FILED_IN_MAY, 100),
        (FILED_IN_MAY + timedelta(days=90), 80),
    ):
        add_fundamentals(
            security_id,
            statement_type=CanonicalStatementType.INCOME_STATEMENT,
            fiscal_period="Q1-2025",
            fiscal_period_end=MARCH_QUARTER_END,
            available_at=available_at,
            data={"revenue": revenue},
        )

    original = client.get(
        "/terminal/companies/ALX/fundamentals",
        params={"as_of": FILED_IN_MAY + timedelta(days=30)},
    ).json()
    revised = client.get("/terminal/companies/ALX/fundamentals", params={"as_of": NOW}).json()

    assert original["statements"]["INCOME_STATEMENT"]["data"]["revenue"] == 100
    assert revised["statements"]["INCOME_STATEMENT"]["data"]["revenue"] == 80


def test_a_later_quarters_filing_does_not_lose_to_an_older_quarters_restatement(
    client, register, add_fundamentals
):
    """Module 07's `precedence` correction, from the API's side.

    Q1 filed in May, Q2 filed in August, Q1 *restated* in September. An
    October query must return Q2 — ordering on availability alone returns
    the Q1 restatement, which is the wrong quarter rather than a stale
    one.
    """
    security_id = register("QBTS")
    add_fundamentals(
        security_id,
        statement_type=CanonicalStatementType.INCOME_STATEMENT,
        fiscal_period="Q1-2025",
        fiscal_period_end=MARCH_QUARTER_END,
        available_at=FILED_IN_MAY,
        data={"revenue": 100, "quarter": "Q1"},
    )
    add_fundamentals(
        security_id,
        statement_type=CanonicalStatementType.INCOME_STATEMENT,
        fiscal_period="Q2-2025",
        fiscal_period_end=datetime(2025, 6, 30, tzinfo=UTC),
        available_at=datetime(2025, 8, 14, tzinfo=UTC),
        data={"revenue": 200, "quarter": "Q2"},
    )
    add_fundamentals(
        security_id,
        statement_type=CanonicalStatementType.INCOME_STATEMENT,
        fiscal_period="Q1-2025",
        fiscal_period_end=MARCH_QUARTER_END,
        available_at=datetime(2025, 9, 10, tzinfo=UTC),
        data={"revenue": 90, "quarter": "Q1-restated"},
    )

    payload = client.get(
        "/terminal/companies/QBTS/fundamentals",
        params={"as_of": datetime(2025, 10, 1, tzinfo=UTC)},
    ).json()

    assert payload["statements"]["INCOME_STATEMENT"]["data"]["quarter"] == "Q2"


def test_an_unknown_ticker_is_a_404_with_a_machine_readable_code(client):
    response = client.get("/terminal/companies/ZZZZ/fundamentals")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SECURITY_NOT_FOUND"
    assert response.json()["error"]["detail"]["ticker"] == "ZZZZ"


def test_a_known_ticker_with_no_data_is_a_200_not_a_404(client, register):
    """A different fact from a bad ticker, and a consumer has to be able
    to tell them apart — which a 404 for both would prevent."""
    register("EMPTY")

    response = client.get("/terminal/companies/EMPTY/fundamentals", params={"as_of": NOW})
    payload = response.json()

    assert response.status_code == 200
    assert payload["statements"] == {}
    assert len(payload["unavailable"]) == 3


# --------------------------------------------------------------------------
# Valuation
# --------------------------------------------------------------------------


def test_valuation_merges_stored_metrics_and_says_where_each_came_from(
    client, register, add_fundamentals
):
    security_id = register("VAL")
    add_fundamentals(
        security_id,
        statement_type=CanonicalStatementType.KEY_METRICS,
        fiscal_period="Q1-2025",
        fiscal_period_end=MARCH_QUARTER_END,
        available_at=FILED_IN_MAY,
        data={"market_cap": 5_000_000},
    )
    add_fundamentals(
        security_id,
        statement_type=CanonicalStatementType.RATIOS,
        fiscal_period="Q1-2025",
        fiscal_period_end=MARCH_QUARTER_END,
        available_at=FILED_IN_MAY,
        data={"price_earnings_ratio": 18.4},
    )

    payload = client.get("/terminal/companies/VAL/valuation", params={"as_of": NOW}).json()

    assert payload["metrics"]["market_cap"] == 5_000_000
    assert payload["metrics"]["price_earnings_ratio"] == 18.4
    assert payload["sources"]["market_cap"] == "KEY_METRICS"
    assert payload["sources"]["price_earnings_ratio"] == "RATIOS"


def test_valuation_computes_nothing_it_was_not_given(client, register, add_fundamentals):
    """A ratio derived here would be a number no other part of ARGUS could
    reproduce, with no availability_time and no lineage."""
    security_id = register("NOCALC")
    add_fundamentals(
        security_id,
        statement_type=CanonicalStatementType.KEY_METRICS,
        fiscal_period="Q1-2025",
        fiscal_period_end=MARCH_QUARTER_END,
        available_at=FILED_IN_MAY,
        data={"market_cap": 100, "net_income": 10},
    )

    payload = client.get("/terminal/companies/NOCALC/valuation", params={"as_of": NOW}).json()

    assert set(payload["metrics"]) == {"market_cap", "net_income"}
    assert "price_earnings_ratio" not in payload["metrics"]


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------


def test_news_returns_articles_newest_first(client, register, add_news):
    security_id = register("NEWS")
    for day, headline in ((1, "oldest"), (5, "middle"), (9, "newest")):
        add_news(
            security_id,
            headline=headline,
            published_at=datetime(2026, 2, day, tzinfo=UTC),
            url=f"https://example.test/{headline}",
        )

    payload = client.get("/terminal/companies/NEWS/news", params={"as_of": NOW}).json()

    assert [article["headline"] for article in payload["articles"]] == [
        "newest",
        "middle",
        "oldest",
    ]
    assert payload["ever_ingested"] is True


def test_an_article_published_after_the_cutoff_is_invisible(client, register, add_news):
    security_id = register("PITNEWS")
    add_news(
        security_id,
        headline="later",
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
        url="https://example.test/later",
    )

    payload = client.get("/terminal/companies/PITNEWS/news", params={"as_of": NOW}).json()

    assert payload["articles"] == []
    # But ARGUS *does* carry news for this name — a different fact from
    # having none, and the one `ever_ingested` exists to express.
    assert payload["ever_ingested"] is True


def test_a_security_with_no_news_at_all_says_so_distinctly(client, register):
    """Today's honest answer for essentially every security: no news has
    been ingested because Module 04's fetcher has never run against a live
    key."""
    register("NONEWS")

    payload = client.get("/terminal/companies/NONEWS/news", params={"as_of": NOW}).json()

    assert payload["articles"] == []
    assert payload["ever_ingested"] is False


def test_the_news_page_size_is_bounded(client, register, add_news):
    security_id = register("MANY")
    for day in range(1, 21):
        add_news(
            security_id,
            headline=f"story-{day}",
            published_at=datetime(2026, 2, 1, tzinfo=UTC) + timedelta(days=day),
            url=f"https://example.test/{day}",
        )

    payload = client.get("/terminal/companies/MANY/news", params={"as_of": NOW, "limit": 5}).json()

    assert len(payload["articles"]) == 5


def test_news_is_append_only_like_every_other_canonical_table(connection, register, add_news):
    """A published article is a historical fact. A correction is a new
    article; silently editing one would rewrite what ARGUS could have
    known at a past instant."""
    import pytest
    from sqlalchemy import text as sql_text

    security_id = register("IMMUTABLE")
    add_news(
        security_id,
        headline="as published",
        published_at=datetime(2026, 2, 1, tzinfo=UTC),
        url="https://example.test/immutable",
    )

    with pytest.raises(Exception) as exc_info:
        connection.execute(
            sql_text("UPDATE canonical_news SET headline = 'rewritten' WHERE security_id = :s"),
            {"s": security_id},
        )
    assert "append-only" in str(exc_info.value)
