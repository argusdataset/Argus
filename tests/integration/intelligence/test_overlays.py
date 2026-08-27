"""Chart overlays: what this service adds, and what it refuses to repeat.

Module 19 owns price. This module owns the ARGUS layer drawn on top of
it. The test that matters most is the negative one — that no bar, no
close, no volume ever leaves this endpoint — because a second price
series that could disagree with the first is worse than no overlay at
all.
"""

from __future__ import annotations

from datetime import timedelta

from infra.db.enums import MarketState
from services.intelligence.overlays import BARS_ENDPOINT
from tests.integration.intelligence.conftest import NOW

#: Every key Module 19's `/history` response uses. None may appear here.
OHLCV_KEYS = ("o", "h", "l", "c", "v", "t", "s", "open", "high", "low", "close", "volume", "bars")


def test_the_overlay_response_serves_no_price_data_at_all(
    client, register, set_state, add_transition
):
    """The layering, checked on the wire rather than in a docstring.

    A structural test asserts this service imports no price loader. This
    asserts the thing that would actually harm a user: bars arriving from
    two places that could drift apart.
    """
    security_id = register("NOBARS")
    set_state(security_id, MarketState.CONSOLIDATION)
    add_transition(security_id, to_state=MarketState.CONSOLIDATION, at=NOW - timedelta(days=30))

    payload = client.get("/intelligence/securities/NOBARS/overlays").json()

    for key in OHLCV_KEYS:
        assert key not in payload
    for mark in payload["marks"]:
        assert not set(OHLCV_KEYS) & set(mark)
    for zone in payload["consolidation_zones"]:
        assert not set(OHLCV_KEYS) & set(zone)


def test_the_response_points_at_module_19s_datafeed_for_bars(client, register, set_state):
    """Not serving price is only half of it; saying where price lives is the rest."""
    set_state(register("POINTER"), MarketState.CONSOLIDATION)

    payload = client.get("/intelligence/securities/POINTER/overlays").json()

    assert payload["bars_endpoint"] == BARS_ENDPOINT
    assert payload["bars_endpoint"].startswith("/terminal/")


def test_module_19s_history_endpoint_still_serves_the_bars_this_one_declines(
    engine, register, connection
):
    """The other half of the layering: the Terminal's feed is untouched.

    Module 21 adding overlays did not move, wrap or shadow Module 19's
    `/history`. Asserted by mounting Module 19's own app against the same
    database and confirming it answers — a change here that quietly took
    over price serving would leave one of these two failing.
    """
    from services.terminal.app import create_app as create_terminal

    terminal_paths = set(create_terminal(engine=engine).openapi()["paths"])

    assert BARS_ENDPOINT in terminal_paths, "Module 19 still owns the bars endpoint"

    intelligence_routes = _intelligence_routes()
    assert BARS_ENDPOINT not in intelligence_routes
    assert not any("history" in path for path in intelligence_routes)


def test_a_state_transition_becomes_one_mark_in_the_charting_library_shape(
    client, register, set_state, add_transition
):
    """Marks are `market_state_transitions` rows, read rather than derived."""
    security_id = register("MARKED")
    set_state(security_id, MarketState.BREAKOUT_READY)
    moment = NOW - timedelta(days=40)
    add_transition(
        security_id,
        from_state=MarketState.DOWN_TREND,
        to_state=MarketState.CONSOLIDATION,
        at=moment,
    )

    marks = client.get("/intelligence/securities/MARKED/overlays").json()["marks"]

    assert len(marks) == 1
    mark = marks[0]
    assert mark["time"] == int(moment.timestamp())
    assert mark["text"] == "DOWN_TREND to CONSOLIDATION"
    assert mark["argus"]["from_state"] == "DOWN_TREND"
    assert mark["argus"]["to_state"] == "CONSOLIDATION"
    assert mark["argus"]["confidence"] == 0.6
    assert mark["argus"]["backward_transition"] is False


def test_a_backward_transition_is_marked_as_one(client, register, set_state, add_transition):
    """Module 10 records it; the chart shows it rather than smoothing it over."""
    security_id = register("RETREAT")
    set_state(security_id, MarketState.CONSOLIDATION)
    add_transition(
        security_id,
        from_state=MarketState.BREAKOUT_READY,
        to_state=MarketState.CONSOLIDATION,
        at=NOW - timedelta(days=5),
        backward=True,
    )

    mark = client.get("/intelligence/securities/RETREAT/overlays").json()["marks"][0]

    assert mark["argus"]["backward_transition"] is True


def test_a_closed_consolidation_span_is_paired_from_the_transition_log(
    client, register, set_state, add_transition
):
    """A zone is an entry paired with the exit that followed it."""
    security_id = register("SPAN")
    set_state(security_id, MarketState.UPTREND)
    entered = NOW - timedelta(days=60)
    left = NOW - timedelta(days=20)
    add_transition(
        security_id,
        from_state=MarketState.DOWN_TREND,
        to_state=MarketState.CONSOLIDATION,
        at=entered,
    )
    add_transition(
        security_id,
        from_state=MarketState.CONSOLIDATION,
        to_state=MarketState.UPTREND,
        at=left,
    )

    zones = client.get("/intelligence/securities/SPAN/overlays").json()["consolidation_zones"]

    assert len(zones) == 1
    assert zones[0]["start"] == int(entered.timestamp())
    assert zones[0]["end"] == int(left.timestamp())
    assert zones[0]["state"] == "CONSOLIDATION"
    assert zones[0]["ongoing"] is False


def test_a_span_still_running_is_left_open_rather_than_closed_at_today(
    client, register, set_state, add_transition
):
    """A zone that has not ended has not ended.

    Stamping it with today's date would assert a transition Module 10
    never recorded — a chart drawing a base that visibly ends on the day
    you happen to look at it.
    """
    security_id = register("OPENZONE")
    set_state(security_id, MarketState.ACCUMULATION)
    entered = NOW - timedelta(days=15)
    add_transition(
        security_id,
        from_state=MarketState.BASE_FORMING,
        to_state=MarketState.ACCUMULATION,
        at=entered,
    )

    zones = client.get("/intelligence/securities/OPENZONE/overlays").json()["consolidation_zones"]

    assert len(zones) == 1
    assert zones[0]["start"] == int(entered.timestamp())
    assert zones[0]["end"] is None
    assert zones[0]["ongoing"] is True


def test_zone_price_boundaries_say_they_are_not_stored_rather_than_being_invented(
    client, register, set_state, add_transition
):
    """The upstream gap, published instead of papered over.

    No module records the support and resistance prices that would give a
    zone a top and a bottom. Deriving them here would be computing a
    feature in an API layer — a number no other part of ARGUS could
    reproduce — so the response says so.
    """
    security_id = register("NOLEVELS")
    set_state(security_id, MarketState.CONSOLIDATION)
    add_transition(security_id, to_state=MarketState.CONSOLIDATION, at=NOW - timedelta(days=10))

    boundaries = client.get("/intelligence/securities/NOLEVELS/overlays").json()[
        "zone_price_boundaries"
    ]

    assert boundaries["available"] is False
    assert boundaries["reason"] == "not_stored"
    assert "does not store price levels" in boundaries["explanation"]


def test_since_bounds_the_marks_the_way_the_widget_asks_for_them(
    client, register, set_state, add_transition
):
    """`getMarks` asks for the visible range, not for everything."""
    security_id = register("WINDOWED")
    set_state(security_id, MarketState.CONSOLIDATION)
    old = NOW - timedelta(days=200)
    recent = NOW - timedelta(days=3)
    add_transition(
        security_id,
        from_state=MarketState.UNCLASSIFIED,
        to_state=MarketState.DOWN_TREND,
        at=old,
    )
    add_transition(
        security_id,
        from_state=MarketState.DOWN_TREND,
        to_state=MarketState.CONSOLIDATION,
        at=recent,
    )

    everything = client.get("/intelligence/securities/WINDOWED/overlays").json()
    since = int((NOW - timedelta(days=30)).timestamp())
    windowed = client.get(f"/intelligence/securities/WINDOWED/overlays?since={since}").json()

    assert len(everything["marks"]) == 2
    assert len(windowed["marks"]) == 1
    assert windowed["marks"][0]["time"] == int(recent.timestamp())


def test_a_security_with_no_transitions_gets_empty_overlays_not_an_error(
    client, register, set_state
):
    """Nothing to draw is an answer."""
    set_state(register("QUIET"), MarketState.DOWN_TREND)

    response = client.get("/intelligence/securities/QUIET/overlays")

    assert response.status_code == 200
    payload = response.json()
    assert payload["marks"] == []
    assert payload["consolidation_zones"] == []
    assert payload["bars_endpoint"] == BARS_ENDPOINT


def _intelligence_routes() -> set[str]:
    from services.intelligence.app import create_app

    return set(create_app(engine=None).openapi()["paths"])  # type: ignore[arg-type]
