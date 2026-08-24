"""The TradingView datafeed against real bars.

The protocol is small and the ways to get it subtly wrong are specific:
returning an error where the library expects `no_data`, serving
unadjusted prices so a split shows as a cliff the user reads as a crash,
or advertising a capability that is never served.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.integration.terminal.conftest import HISTORY_START, NOW

FROM = int(HISTORY_START.timestamp())
TO = int(NOW.timestamp())


def _closes(count: int, start: float = 50.0, step: float = 0.25) -> list[float]:
    return [start + step * n for n in range(count)]


# --------------------------------------------------------------------------
# Protocol surface
# --------------------------------------------------------------------------


def test_config_says_what_this_datafeed_can_and_cannot_do(client):
    payload = client.get("/terminal/datafeed/config").json()

    assert payload["supported_resolutions"] == ["1D", "1W", "1M"]
    assert payload["supports_search"] is True
    # Overlays are Module 21's. Advertising them and serving none would
    # make the widget request them forever.
    assert payload["supports_marks"] is False
    assert payload["supports_timescale_marks"] is False


def test_time_returns_unix_seconds(client):
    response = client.get("/terminal/datafeed/time")

    assert response.status_code == 200
    assert isinstance(response.json(), int)
    assert response.json() > 1_600_000_000


def test_symbol_resolution_describes_an_end_of_day_daily_instrument(client, register):
    register("MLSS", name="Milestone Scientific")

    payload = client.get("/terminal/datafeed/symbols", params={"symbol": "MLSS"}).json()

    assert payload["ticker"] == "MLSS"
    assert payload["description"] == "Milestone Scientific"
    assert payload["exchange"] == "NASDAQ"
    # ARGUS holds end-of-day bars. Claiming intraday would have the widget
    # asking for minute bars that can only be fabricated.
    assert payload["has_intraday"] is False
    assert payload["data_status"] == "endofday"
    assert payload["supported_resolutions"] == ["1D", "1W", "1M"]


def test_a_prefixed_symbol_resolves_the_same_as_a_bare_one(client, register):
    """The library sends `NASDAQ:AAPL` once a symbol has been resolved and
    the bare form when a user types it."""
    register("SLS")

    bare = client.get("/terminal/datafeed/symbols", params={"symbol": "SLS"}).json()
    prefixed = client.get("/terminal/datafeed/symbols", params={"symbol": "NASDAQ:SLS"}).json()

    assert bare == prefixed


def test_an_unknown_symbol_is_a_404_with_a_code(client):
    response = client.get("/terminal/datafeed/symbols", params={"symbol": "NOPE"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SECURITY_NOT_FOUND"


def test_search_matches_a_ticker_prefix(client, register):
    register("HIVE")
    register("HIVX")
    register("QBTS")

    results = client.get("/terminal/datafeed/search", params={"query": "HIV"}).json()

    assert {row["ticker"] for row in results} == {"HIVE", "HIVX"}
    assert all(row["full_name"] == f"NASDAQ:{row['ticker']}" for row in results)


def test_search_also_matches_a_company_name(client, register):
    register("ALX", name="Alpha Logic Exchange")

    results = client.get("/terminal/datafeed/search", params={"query": "Alpha Logic"}).json()

    assert [row["ticker"] for row in results] == ["ALX"]


def test_an_empty_query_returns_nothing_rather_than_the_universe(client, register):
    register("ANY")

    assert client.get("/terminal/datafeed/search", params={"query": ""}).json() == []
    assert client.get("/terminal/datafeed/search").json() == []


def test_search_is_bounded(client, register):
    for index in range(40):
        register(f"BULK{index:02d}")

    results = client.get("/terminal/datafeed/search", params={"query": "BULK"}).json()

    assert len(results) <= 30


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def test_history_returns_parallel_arrays_in_the_protocols_shape(client, register, add_bars):
    security_id = register("BARS")
    add_bars(security_id, closes=_closes(60))

    payload = client.get(
        "/terminal/datafeed/history",
        params={"symbol": "BARS", "resolution": "1D", "from": FROM, "to": TO},
    ).json()

    assert payload["s"] == "ok"
    lengths = {len(payload[key]) for key in ("t", "o", "h", "l", "c", "v")}
    assert len(lengths) == 1
    assert lengths.pop() == 60
    # Oldest first — the library assumes ascending time.
    assert payload["t"] == sorted(payload["t"])


def test_history_for_a_range_with_no_bars_is_no_data_not_an_error(client, register, add_bars):
    """The protocol's own distinction. A chart paging backwards past a
    listing date must not render a failure."""
    security_id = register("EARLY")
    add_bars(security_id, closes=_closes(30))

    ancient = int(datetime(2010, 1, 1, tzinfo=UTC).timestamp())
    payload = client.get(
        "/terminal/datafeed/history",
        params={
            "symbol": "EARLY",
            "resolution": "1D",
            "from": ancient,
            "to": ancient + 86_400 * 30,
        },
    ).json()

    assert payload["s"] == "no_data"
    # And it says where data does start, so the widget stops paging.
    assert payload["nextTime"] is not None


def test_history_bounds_the_far_end_of_the_range(client, register, add_bars):
    """Module 07's loader bounds history at the front and at `as_of`. A
    chart asking for a window that *ends* in the past is the one case that
    needs a back bound too."""
    security_id = register("WINDOW")
    add_bars(security_id, closes=_closes(120))

    cutoff = HISTORY_START + timedelta(days=30)
    payload = client.get(
        "/terminal/datafeed/history",
        params={
            "symbol": "WINDOW",
            "resolution": "1D",
            "from": FROM,
            "to": int(cutoff.timestamp()),
        },
    ).json()

    assert payload["s"] == "ok"
    assert max(payload["t"]) <= int(cutoff.timestamp())
    assert len(payload["t"]) < 120


def test_countback_returns_the_newest_bars_not_the_oldest(client, register, add_bars):
    """A chart paging backwards wants the newest end of what it asked
    for. Returning the oldest would show the wrong decade."""
    security_id = register("COUNT")
    add_bars(security_id, closes=_closes(90))

    full = client.get(
        "/terminal/datafeed/history",
        params={"symbol": "COUNT", "resolution": "1D", "from": FROM, "to": TO},
    ).json()
    limited = client.get(
        "/terminal/datafeed/history",
        params={
            "symbol": "COUNT",
            "resolution": "1D",
            "from": FROM,
            "to": TO,
            "countback": 10,
        },
    ).json()

    assert len(limited["t"]) == 10
    assert limited["t"] == full["t"][-10:]


def test_weekly_bars_are_derived_from_daily_and_are_fewer(client, register, add_bars):
    """Only daily is sourced from a provider. Module 08 rolls it up, so
    ARGUS keeps point-in-time control of the aggregation."""
    security_id = register("WEEKLY")
    add_bars(security_id, closes=_closes(120))

    daily = client.get(
        "/terminal/datafeed/history",
        params={"symbol": "WEEKLY", "resolution": "1D", "from": FROM, "to": TO},
    ).json()
    weekly = client.get(
        "/terminal/datafeed/history",
        params={"symbol": "WEEKLY", "resolution": "1W", "from": FROM, "to": TO},
    ).json()

    assert weekly["s"] == "ok"
    assert 0 < len(weekly["t"]) < len(daily["t"])
    # A weekly bar's high is the max of its days, so it cannot be lower.
    assert max(weekly["h"]) <= max(daily["h"]) + 1e-9


def test_an_unrecognised_resolution_falls_back_to_daily_rather_than_failing(
    client, register, add_bars
):
    """A saved chart layout can name a resolution the server never
    advertised. Answering with the nearest thing ARGUS has beats a blank
    screen."""
    security_id = register("FALLBACK")
    add_bars(security_id, closes=_closes(40))

    payload = client.get(
        "/terminal/datafeed/history",
        params={"symbol": "FALLBACK", "resolution": "5", "from": FROM, "to": TO},
    ).json()

    assert payload["s"] == "ok"
    assert len(payload["t"]) == 40


def test_chart_prices_are_split_adjusted(client, register, add_bars, add_split):
    """An unadjusted series has a cliff at every split, and a user reading
    a base-and-breakout structure would see a pattern that never
    happened."""
    security_id = register("SPLIT")
    add_bars(security_id, closes=[100.0] * 60)
    add_split(security_id, effective=HISTORY_START + timedelta(days=40))

    payload = client.get(
        "/terminal/datafeed/history",
        params={"symbol": "SPLIT", "resolution": "1D", "from": FROM, "to": TO},
    ).json()

    closes = payload["c"]
    # Bars before the split are halved; the most recent keeps its raw
    # price, which is Module 08's convention.
    assert closes[0] < closes[-1]
    assert abs(closes[0] - 50.0) < 1e-6
    assert abs(closes[-1] - 100.0) < 1e-6


def test_a_split_announced_later_does_not_touch_an_earlier_view(
    connection, register, add_bars, add_split
):
    """The leak Module 08's panel builder exists to prevent, reachable
    from here only because this module pairs the two loaders correctly.

    At the service level rather than over HTTP: the `/history` endpoint
    takes no `as_of` — a chart is always "now" — but `load_bars` does,
    and it is the function that would carry the bug. An earlier version
    of this test went through the endpoint with an `as_of` query
    parameter it silently ignored, so it asserted nothing.
    """
    from services.terminal.bars import load_bars

    security_id = register("LATESPLIT")
    add_bars(security_id, closes=[100.0] * 60)
    announced = HISTORY_START + timedelta(days=80)
    add_split(
        security_id,
        effective=HISTORY_START + timedelta(days=20),
        available_at=announced,
    )

    before = load_bars(
        connection,
        security_id,
        start=HISTORY_START,
        end=announced - timedelta(days=1),
        as_of=announced - timedelta(days=1),
    )
    after = load_bars(
        connection,
        security_id,
        start=HISTORY_START,
        end=NOW,
        as_of=NOW,
    )

    # Before the announcement the split is invisible, so nothing is
    # adjusted and every close is the raw 100.
    assert before.closes[0] == 100.0
    # Afterwards the pre-split bars are halved.
    assert after.closes[0] == 50.0


def test_history_for_an_unknown_symbol_is_a_404(client):
    response = client.get(
        "/terminal/datafeed/history",
        params={"symbol": "GHOST", "resolution": "1D", "from": FROM, "to": TO},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SECURITY_NOT_FOUND"
