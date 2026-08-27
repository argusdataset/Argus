"""The three derived watchlists, against a real `market_state` projection.

The claim these tests exist to check is narrow and load-bearing: a
derived watchlist has no storage, so it cannot disagree with the state it
is derived from. Everything else about the endpoint — its shape, its
bounds, its refusals — matters less than that.
"""

from __future__ import annotations

from datetime import timedelta

from infra.db.enums import MarketState
from tests.integration.intelligence.conftest import NOW


def test_the_three_names_are_the_ones_module_10_defines(client):
    """The set is closed upstream, not restated here."""
    response = client.get("/intelligence/watchlists")

    assert response.status_code == 200
    assert response.json() == ["DOWN_TREND", "CONSOLIDATION", "BREAKOUT_READY"]


def test_a_state_change_moves_a_security_between_lists_with_no_refresh(client, register, set_state):
    """The guarantee: no cache, so no window where the two disagree.

    The same security is read three times in one test with a state
    change between each read and no refresh, reindex or invalidation
    step in between — because there is nothing to refresh. A cache
    introduced later would fail this at the second read.
    """
    security_id = register("MOVER")

    set_state(security_id, MarketState.DOWN_TREND)
    assert _ids(client, "DOWN_TREND") == [str(security_id)]
    assert _ids(client, "CONSOLIDATION") == []

    set_state(security_id, MarketState.CONSOLIDATION)
    assert _ids(client, "DOWN_TREND") == []
    assert _ids(client, "CONSOLIDATION") == [str(security_id)]

    set_state(security_id, MarketState.BREAKOUT_READY)
    assert _ids(client, "CONSOLIDATION") == []
    assert _ids(client, "BREAKOUT_READY") == [str(security_id)]


def test_a_state_on_no_list_removes_the_security_from_all_three(client, register, set_state):
    """UPTREND is not a watchlist state, and nothing here filters it out.

    Absence comes from the state mapping rather than from an exclusion
    this module applies — which is the stronger arrangement, because a
    filter can be forgotten and a missing mapping entry cannot.
    """
    security_id = register("GONE")
    set_state(security_id, MarketState.BREAKOUT_READY)
    assert _ids(client, "BREAKOUT_READY") == [str(security_id)]

    set_state(security_id, MarketState.UPTREND)

    for name in ("DOWN_TREND", "CONSOLIDATION", "BREAKOUT_READY"):
        assert _ids(client, name) == []


def test_each_list_covers_exactly_the_states_module_10_maps_to_it(client, register, set_state):
    """Both states in a pair land on the same list, and only that list."""
    pairs = {
        "DOWN_TREND": (MarketState.DOWN_TREND, MarketState.BASE_FORMING),
        "CONSOLIDATION": (MarketState.CONSOLIDATION, MarketState.ACCUMULATION),
        "BREAKOUT_READY": (MarketState.BREAKOUT_WATCH, MarketState.BREAKOUT_READY),
    }
    expected: dict[str, set[str]] = {}
    for index, (name, states) in enumerate(pairs.items()):
        expected[name] = set()
        for offset, state in enumerate(states):
            security_id = register(f"PAIR{index}{offset}")
            set_state(security_id, state)
            expected[name].add(str(security_id))

    for name, states in pairs.items():
        response = client.get(f"/intelligence/watchlists/{name}")
        payload = response.json()

        assert set(payload["states"]) == {state.value for state in states}
        assert set(_ids(client, name)) == expected[name]
        assert payload["count"] == len(expected[name])


def test_a_watchlist_says_on_the_wire_that_it_is_not_stored(client, register, set_state):
    """`stored: false` is published, not implied.

    A client caching this is caching a view of something that moves, and a
    client comparing it to a Module 19 User Watchlist should be able to
    see the two are different kinds of thing without reading a document.
    """
    set_state(register("NOTSTORED"), MarketState.CONSOLIDATION)

    payload = client.get("/intelligence/watchlists/CONSOLIDATION").json()

    assert payload["stored"] is False


def test_an_entry_carries_identity_the_state_and_the_score_block(
    client, register, set_state, score
):
    """What an entry is: security, state, when it entered, what ARGUS thinks."""
    security_id = register("ENTRY", name="Entry Industries")
    set_state(security_id, MarketState.CONSOLIDATION, entered_at=NOW - timedelta(days=4))
    score(security_id)

    entry = client.get("/intelligence/watchlists/CONSOLIDATION").json()["entries"][0]

    assert entry["security_id"] == str(security_id)
    assert entry["ticker"] == "ENTRY"
    assert entry["name"] == "Entry Industries"
    assert entry["state"] == "CONSOLIDATION"
    assert entry["entered_at"].startswith((NOW - timedelta(days=4)).strftime("%Y-%m-%d"))
    assert entry["state_confidence"] == 0.6
    assert "scored" in entry["score"]


def test_the_limit_bounds_the_payload_and_not_the_count(client, register, set_state):
    """`count` stays the full membership so a first page knows its size."""
    for index in range(5):
        set_state(register(f"MANY{index}"), MarketState.CONSOLIDATION)

    payload = client.get("/intelligence/watchlists/CONSOLIDATION?limit=2").json()

    assert len(payload["entries"]) == 2
    assert payload["count"] == 5


def test_an_unknown_watchlist_names_the_three_that_exist(client):
    """A 404 that tells the caller what they could have asked for."""
    response = client.get("/intelligence/watchlists/MOMENTUM")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "UNKNOWN_WATCHLIST"
    assert error["detail"]["available"] == ["DOWN_TREND", "CONSOLIDATION", "BREAKOUT_READY"]


def test_an_empty_watchlist_is_an_empty_list_and_not_an_error(client):
    """Nothing consolidating is an answer, and a common one."""
    response = client.get("/intelligence/watchlists/CONSOLIDATION")

    assert response.status_code == 200
    payload = response.json()
    assert payload["entries"] == []
    assert payload["count"] == 0
    assert payload["freshness"]["stale"] is False


def _ids(client, name: str) -> list[str]:
    response = client.get(f"/intelligence/watchlists/{name}")
    assert response.status_code == 200, response.text
    return [entry["security_id"] for entry in response.json()["entries"]]
