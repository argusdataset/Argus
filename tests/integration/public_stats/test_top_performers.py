"""Top Performers: the fifth, curated output, against real approved data.

The assertions that matter most here are the ones that keep this from
becoming a second, quieter success definition or a cherry-picked page in
disguise: the threshold narrows an already-SUCCESS outcome rather than
redefining success, the gate applies exactly as it does to the other
four, zero qualifying outcomes is reported honestly, and no entry carries
a security's identity.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from infra.db.enums import OutcomeStatus
from services.public_stats.aggregates import CHART_TOP_PERFORMERS, CHARTS
from services.public_stats.snapshots import refresh_public_stats
from tests.integration.public_stats.conftest import AS_OF, PERIOD_START


@pytest.fixture
def published(connection, make_run, reviewer):
    """An approved run, and a refresh once outcomes have been seeded."""

    def _publish():
        refresh_public_stats(connection, as_of=AS_OF)

    make_run(status="APPROVED", reviewer_id=reviewer)
    return _publish


def test_top_performers_is_one_of_the_five_published_charts():
    assert CHART_TOP_PERFORMERS in CHARTS
    assert CHARTS[-1] == CHART_TOP_PERFORMERS


# --------------------------------------------------------------------------
# The filter narrows SUCCESS; it does not redefine it
# --------------------------------------------------------------------------


def test_only_success_outcomes_at_or_above_the_threshold_qualify(client, published, seed_outcomes):
    seed_outcomes(3, prefix="BIG", outcome_status=OutcomeStatus.SUCCESS, relative_return=0.60)
    seed_outcomes(
        3,
        prefix="MED",
        outcome_status=OutcomeStatus.SUCCESS,
        relative_return=0.10,
        detected_from=PERIOD_START + timedelta(days=10),
    )
    published()

    payload = client.get("/public/stats/top_performers").json()

    # relative_return=0.60 -> realized_return=0.65 (fixture adds 0.05).
    assert payload["sample_size"] == 3
    assert all(row["realized_return"] >= 0.50 for row in payload["series"])


def test_a_success_outcome_below_the_threshold_does_not_qualify(client, published, seed_outcomes):
    """The bar is real — not everyone who succeeded is a "top performer"."""
    seed_outcomes(5, prefix="OK", outcome_status=OutcomeStatus.SUCCESS, relative_return=0.20)
    published()

    payload = client.get("/public/stats/top_performers").json()

    assert payload["sample_size"] == 0
    assert payload["series"] == []


def test_a_large_return_that_is_not_classified_success_does_not_qualify(
    client, published, seed_outcomes
):
    """This endpoint filters and displays; it never redefines SUCCESS.

    A setup Module 15 classified as FAILED does not become a "top
    performer" no matter what its realized return column says — the
    outcome_status ARGUS already assigned is the door, and this endpoint
    does not open a second one.
    """
    seed_outcomes(
        4,
        prefix="NOTSUCCESS",
        outcome_status=OutcomeStatus.INVALIDATED,
        relative_return=0.80,
    )
    published()

    payload = client.get("/public/stats/top_performers").json()

    assert payload["sample_size"] == 0
    assert payload["series"] == []


def test_entries_are_sorted_by_realized_return_descending(client, published, seed_outcomes):
    seed_outcomes(1, prefix="LOW", outcome_status=OutcomeStatus.SUCCESS, relative_return=0.50)
    seed_outcomes(
        1,
        prefix="HIGH",
        outcome_status=OutcomeStatus.SUCCESS,
        relative_return=1.20,
        detected_from=PERIOD_START + timedelta(days=5),
    )
    seed_outcomes(
        1,
        prefix="MID",
        outcome_status=OutcomeStatus.SUCCESS,
        relative_return=0.70,
        detected_from=PERIOD_START + timedelta(days=10),
    )
    published()

    returns = [
        row["realized_return"]
        for row in client.get("/public/stats/top_performers").json()["series"]
    ]

    assert returns == sorted(returns, reverse=True)


def test_the_limit_caps_what_is_shown_but_not_the_qualifying_count(
    client, published, seed_outcomes
):
    for index in range(25):
        seed_outcomes(
            1,
            prefix=f"MANY{index:02d}",
            outcome_status=OutcomeStatus.SUCCESS,
            relative_return=0.55 + index * 0.01,
            detected_from=PERIOD_START + timedelta(days=index),
        )
    published()

    payload = client.get("/public/stats/top_performers").json()

    assert payload["summary"]["qualifying_count"] == 25
    assert payload["sample_size"] == 25
    assert len(payload["series"]) == 20  # top_performer_limit's default


# --------------------------------------------------------------------------
# No security identity travels with an entry
# --------------------------------------------------------------------------


def test_no_entry_carries_a_ticker_or_security_id(client, published, seed_outcomes):
    """A return figure, not a stock pick.

    Every other public number in this module is an aggregate over many
    outcomes; naming which company produced one particular return would
    make this the one place the page singled out an individual case.
    """
    seed_outcomes(3, prefix="ANON", outcome_status=OutcomeStatus.SUCCESS, relative_return=0.75)
    published()

    payload = client.get("/public/stats/top_performers").json()

    assert payload["series"]
    for row in payload["series"]:
        assert "ticker" not in row
        assert "security_id" not in row
        assert "name" not in row
        assert set(row) == {"realized_return", "mfe", "concluded_at"}


# --------------------------------------------------------------------------
# The zero-qualifying state is honest, not an error and not a fabrication
# --------------------------------------------------------------------------


def test_zero_qualifying_results_is_a_200_with_an_honest_caption(client, published, seed_outcomes):
    """A real, expected state early on — not an error, not an empty-looking
    placeholder with no explanation."""
    seed_outcomes(10, prefix="NONE", outcome_status=OutcomeStatus.SUCCESS, relative_return=0.05)
    published()

    response = client.get("/public/stats/top_performers")
    payload = response.json()

    assert response.status_code == 200
    assert payload["sample_size"] == 0
    assert payload["series"] == []
    assert "50%" in payload["caption"]
    assert payload["caption"]  # never blank


def test_no_published_outcomes_at_all_is_also_honest(client, published):
    published()  # refreshed with nothing seeded

    payload = client.get("/public/stats/top_performers").json()

    assert payload["sample_size"] == 0
    assert "No published outcomes yet" in payload["caption"]


# --------------------------------------------------------------------------
# Labelling: never mistakable for the complete picture
# --------------------------------------------------------------------------


def test_the_response_is_labelled_as_a_curated_subset(client, published, seed_outcomes):
    seed_outcomes(2, prefix="LBL", outcome_status=OutcomeStatus.SUCCESS, relative_return=0.60)
    published()

    payload = client.get("/public/stats/top_performers").json()

    assert payload["summary"]["kind"] == "curated_subset"
    assert payload["summary"]["threshold"] == pytest.approx(0.50)
    assert "curated highlight" in payload["caption"]
    assert "full, unfiltered distribution" in payload["caption"]


# --------------------------------------------------------------------------
# The review-gate boundary — identical to the other four
# --------------------------------------------------------------------------


def test_top_performers_shows_nothing_from_an_unreviewed_run(
    connection, client, make_run, seed_outcomes
):
    """Same gate, same door. A run nobody queued for review is not
    reachable through any chart this module serves, this one included."""
    make_run()
    seed_outcomes(5, prefix="UNREV", outcome_status=OutcomeStatus.SUCCESS, relative_return=0.90)
    refresh_public_stats(connection, as_of=AS_OF)

    payload = client.get("/public/stats/top_performers").json()

    assert payload["sample_size"] == 0


def test_top_performers_is_withdrawn_when_its_run_is_rejected(
    connection, client, make_run, reviewer, seed_outcomes
):
    """The strongest gate test, mirrored from `test_review_gate.py`: a run
    approved, published, then rejected must not keep serving through this
    chart either.
    """
    from core.model_validation_evaluation.validation.review import reject

    run_id = make_run(status="APPROVED", reviewer_id=reviewer)
    seed_outcomes(5, prefix="WD", outcome_status=OutcomeStatus.SUCCESS, relative_return=0.90)
    refresh_public_stats(connection, as_of=AS_OF)

    populated = client.get("/public/stats/top_performers").json()
    assert populated["sample_size"] == 5

    reject(connection, run_id, user_id=reviewer, note="withdrawing")

    response = client.get("/public/stats/top_performers")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STATISTICS_WITHDRAWN"


# --------------------------------------------------------------------------
# The four original charts are unaffected
# --------------------------------------------------------------------------


def test_the_four_original_charts_still_serve_unchanged(client, published, seed_outcomes):
    """Adding a fifth output must not shrink, remove or alter the four
    full-statistics endpoints that remain the primary content."""
    seed_outcomes(40, prefix="ORIG")
    published()

    for chart in (
        "win_rate",
        "cumulative_performance",
        "regime_breakdown",
        "excursion_distribution",
    ):
        response = client.get(f"/public/stats/{chart}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["chart"] == chart
        assert payload["sample_size"] == 40


def test_the_summary_endpoint_lists_all_five_charts(client, published, seed_outcomes):
    seed_outcomes(1, prefix="SUM", outcome_status=OutcomeStatus.SUCCESS)
    published()

    payload = client.get("/public/stats").json()

    assert set(payload["charts"]) == {
        "win_rate",
        "cumulative_performance",
        "regime_breakdown",
        "excursion_distribution",
        "top_performers",
    }
