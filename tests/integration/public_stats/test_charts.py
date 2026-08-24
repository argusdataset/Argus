"""The four charts, against real approved data, with mixed sample sizes.

The assertions that matter most are about what the charts *decline* to
say. A public page that showed a 100% win rate from four outcomes would
be technically accurate and profoundly misleading, and the sample floor
is the only thing standing between ARGUS and that page.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from infra.db.enums import MarketState, OutcomeStatus
from services.public_stats.aggregates import CHARTS
from services.public_stats.snapshots import refresh_public_stats
from tests.integration.public_stats.conftest import AS_OF, PERIOD_START


@pytest.fixture
def published(connection, make_run, reviewer):
    """An approved run, and a refresh once outcomes have been seeded."""

    def _publish():
        refresh_public_stats(connection, as_of=AS_OF)

    make_run(status="APPROVED", reviewer_id=reviewer)
    return _publish


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize("chart", CHARTS)
def test_every_chart_serves_with_freshness_and_provenance(client, published, seed_outcomes, chart):
    seed_outcomes(40, prefix="SHP")
    published()

    payload = client.get(f"/public/stats/{chart}").json()

    assert payload["chart"] == chart
    assert payload["caption"]
    assert payload["freshness"]["computed_at"]
    assert payload["freshness"]["as_of"]
    assert payload["freshness"]["stale"] is False
    # Provenance travels with every number, so a reader can check ARGUS is
    # not choosing which periods to count.
    assert len(payload["provenance"]["approved_runs"]) == 1


def test_an_unknown_chart_is_a_404_not_an_empty_chart(client, published, seed_outcomes):
    """ "No such chart" is the caller's mistake; "this chart is empty" is
    a fact about ARGUS. Different answers."""
    seed_outcomes(40, prefix="UNK")
    published()

    response = client.get("/public/stats/sharpe_ratio")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CHART_NOT_FOUND"
    assert "win_rate" in response.json()["error"]["detail"]["available"]


def test_a_response_is_cacheable(client, published, seed_outcomes):
    """The one module expected to take public traffic. An intermediary
    should hold something ARGUS has decided is current."""
    seed_outcomes(40, prefix="CCH")
    published()

    response = client.get("/public/stats/win_rate")

    assert "max-age=" in response.headers["cache-control"]
    assert response.headers["cache-control"].startswith("public")


# --------------------------------------------------------------------------
# 1. Win rate
# --------------------------------------------------------------------------


def test_the_win_rate_chart_shows_every_status_not_just_wins_and_losses(
    client, published, seed_outcomes
):
    """Module 15 established that an expired setup did not fail — it did
    not conclude. Folding it away would report a success rate over a
    denominator ARGUS chose after seeing the results."""
    seed_outcomes(20, prefix="WIN", outcome_status=OutcomeStatus.SUCCESS)
    seed_outcomes(
        12,
        prefix="LOSS",
        outcome_status=OutcomeStatus.FAILED,
        relative_return=-0.10,
        detected_from=PERIOD_START + timedelta(days=30),
    )
    seed_outcomes(
        8,
        prefix="EXP",
        outcome_status=OutcomeStatus.EXPIRED,
        relative_return=0.01,
        detected_from=PERIOD_START + timedelta(days=60),
    )
    published()

    payload = client.get("/public/stats/win_rate").json()
    counts = {row["status"]: row["count"] for row in payload["series"]}

    assert counts == {"SUCCESS": 20, "FAILED": 12, "EXPIRED": 8}
    assert payload["sample_size"] == 40
    # Rates are over resolved setups only; the expired ones are visible
    # but not counted as losses.
    assert payload["summary"]["resolved"] == 32
    assert payload["summary"]["precision"] == pytest.approx(20 / 32)


def test_a_thin_population_shows_a_count_and_refuses_a_rate(client, published, seed_outcomes):
    """The assertion this module exists for. Four wins out of four is a
    100% win rate and it means nothing."""
    seed_outcomes(4, prefix="THIN")
    published()

    payload = client.get("/public/stats/win_rate").json()

    assert payload["sample_size"] == 4
    assert payload["summary"]["precision"] is None
    assert payload["summary"]["hit_rate"] is None
    unavailable = payload["summary"]["unavailable"]
    assert unavailable["available"] is False
    assert unavailable["observed"] == 4
    assert unavailable["required"] == 30
    assert "looks exactly like one computed from thousands" in unavailable["explanation"]


def test_the_counts_are_still_shown_below_the_floor(client, published, seed_outcomes):
    """Refusing the rate is not refusing the data. A reader can see there
    are four outcomes and that three succeeded — they just are not handed
    a percentage."""
    seed_outcomes(3, prefix="CNT", outcome_status=OutcomeStatus.SUCCESS)
    seed_outcomes(
        1,
        prefix="CNTF",
        outcome_status=OutcomeStatus.FAILED,
        detected_from=PERIOD_START + timedelta(days=10),
    )
    published()

    payload = client.get("/public/stats/win_rate").json()
    counts = {row["status"]: row["count"] for row in payload["series"]}

    assert counts == {"SUCCESS": 3, "FAILED": 1}


# --------------------------------------------------------------------------
# 2. Cumulative performance
# --------------------------------------------------------------------------


def test_the_cumulative_series_is_ordered_and_ends_at_the_total(client, published, seed_outcomes):
    seed_outcomes(40, prefix="CUM", relative_return=0.10)
    published()

    payload = client.get("/public/stats/cumulative_performance").json()
    series = payload["series"]

    assert len(series) == 40
    assert [row["date"] for row in series] == sorted(row["date"] for row in series)
    assert series[-1]["cumulative_relative_return"] == pytest.approx(4.0)
    assert payload["summary"]["final_cumulative_relative_return"] == pytest.approx(4.0)


def test_the_cumulative_chart_states_its_basis(client, published, seed_outcomes):
    """Summed benchmark-relative, not compounded absolute. A reader
    comparing this to a broker statement needs to know which."""
    seed_outcomes(40, prefix="BAS")
    published()

    summary = client.get("/public/stats/cumulative_performance").json()["summary"]

    assert "benchmark_relative" in summary["basis"]
    assert "no position sizing" in summary["basis"]


def test_a_drawdown_is_reported_alongside_the_gain(client, published, seed_outcomes):
    """The path, not just the endpoint. A good average built from one
    lucky stretch should be visible as one lucky stretch."""
    seed_outcomes(20, prefix="UP", relative_return=0.10)
    seed_outcomes(
        20,
        prefix="DOWN",
        relative_return=-0.15,
        outcome_status=OutcomeStatus.FAILED,
        detected_from=PERIOD_START + timedelta(days=30),
    )
    published()

    summary = client.get("/public/stats/cumulative_performance").json()["summary"]

    assert summary["max_drawdown"] < 0
    assert summary["max_drawdown"] == pytest.approx(-3.0)


def test_a_single_point_is_not_drawn_as_a_line(client, published, seed_outcomes):
    """One point is not a trend, and drawing it as one would be the chart
    lying about what it knows."""
    seed_outcomes(1, prefix="ONE")
    published()

    payload = client.get("/public/stats/cumulative_performance").json()

    assert payload["series"] == []
    assert payload["summary"]["unavailable"]["available"] is False


# --------------------------------------------------------------------------
# 3. Regime breakdown
# --------------------------------------------------------------------------


def test_regimes_are_reported_separately_with_their_own_sufficiency(
    client, published, seed_outcomes
):
    """A regime with nine outcomes is a fact about where ARGUS has no
    evidence. Dropping it would imply the system has been tested
    everywhere it has not."""
    seed_outcomes(35, prefix="UPT", regime=MarketState.UPTREND)
    seed_outcomes(
        9,
        prefix="DWN",
        regime=MarketState.DOWN_TREND,
        relative_return=-0.05,
        outcome_status=OutcomeStatus.FAILED,
        detected_from=PERIOD_START + timedelta(days=40),
    )
    published()

    payload = client.get("/public/stats/regime_breakdown").json()
    rows = {row["regime"]: row for row in payload["series"]}

    assert rows["UPTREND"]["sufficiency"] == "ADEQUATE"
    assert rows["UPTREND"]["hit_rate"] is not None
    assert rows["DOWN_TREND"]["sample_size"] == 9
    assert rows["DOWN_TREND"]["sufficiency"] == "INSUFFICIENT"
    assert rows["DOWN_TREND"]["hit_rate"] is None
    assert rows["DOWN_TREND"]["unavailable"]["required"] == 30
    assert payload["summary"]["reportable_regimes"] == 1
    assert payload["summary"]["regimes"] == 2


def test_the_regime_caption_says_how_much_is_reportable(client, published, seed_outcomes):
    seed_outcomes(35, prefix="RC", regime=MarketState.UPTREND)
    seed_outcomes(
        5,
        prefix="RCD",
        regime=MarketState.DISTRIBUTION,
        detected_from=PERIOD_START + timedelta(days=40),
    )
    published()

    caption = client.get("/public/stats/regime_breakdown").json()["caption"]

    assert "1 of 2 market regime(s)" in caption


# --------------------------------------------------------------------------
# 4. MFE / MAE distribution
# --------------------------------------------------------------------------


def test_the_excursion_chart_returns_two_histograms(client, published, seed_outcomes):
    seed_outcomes(40, prefix="EXC", mfe=0.30, mae=-0.10)
    published()

    payload = client.get("/public/stats/excursion_distribution").json()
    measures = {row["measure"]: row for row in payload["series"]}

    assert set(measures) == {"mfe", "mae"}
    assert sum(b["count"] for b in measures["mfe"]["bins"]) == 40
    assert sum(b["count"] for b in measures["mae"]["bins"]) == 40


def test_the_histogram_axis_is_the_same_shape_regardless_of_sample_size(
    client, published, seed_outcomes
):
    """An axis that changed with the data would make two charts impossible
    to compare by eye."""
    seed_outcomes(40, prefix="AX")
    published()

    payload = client.get("/public/stats/excursion_distribution").json()
    measures = {row["measure"]: row for row in payload["series"]}

    assert len(measures["mfe"]["bins"]) == 12
    assert len(measures["mae"]["bins"]) == 12
    assert measures["mfe"]["bins"][0]["open_low"] is True
    assert measures["mfe"]["bins"][-1]["open_high"] is True


def test_an_extreme_excursion_lands_in_the_edge_bin_rather_than_being_dropped(
    client, published, seed_outcomes
):
    """Every published outcome is counted. Clipping is about the axis, not
    about which results ARGUS shows."""
    seed_outcomes(39, prefix="NORM", mfe=0.30)
    seed_outcomes(
        1,
        prefix="MOON",
        mfe=40.0,
        detected_from=PERIOD_START + timedelta(days=45),
    )
    published()

    payload = client.get("/public/stats/excursion_distribution").json()
    mfe_bins = next(row for row in payload["series"] if row["measure"] == "mfe")["bins"]

    assert sum(b["count"] for b in mfe_bins) == 40
    assert mfe_bins[-1]["count"] == 1
    assert mfe_bins[-1]["open_high"] is True


def test_the_excursion_medians_respect_the_floor(client, published, seed_outcomes):
    seed_outcomes(5, prefix="EM")
    published()

    summary = client.get("/public/stats/excursion_distribution").json()["summary"]

    assert summary["median_mfe"] is None
    assert summary["median_mae"] is None
    assert summary["unavailable"]["observed"] == 5


# --------------------------------------------------------------------------
# Consistency across charts
# --------------------------------------------------------------------------


def test_all_four_charts_agree_on_how_many_outcomes_exist(client, published, seed_outcomes):
    """Computed from one dataset per refresh, so two charts on the same
    page cannot disagree about the population behind them."""
    seed_outcomes(40, prefix="AGR")
    published()

    sizes = {
        chart: client.get(f"/public/stats/{chart}").json()["sample_size"]
        for chart in ("win_rate", "cumulative_performance", "excursion_distribution")
    }

    assert set(sizes.values()) == {40}
