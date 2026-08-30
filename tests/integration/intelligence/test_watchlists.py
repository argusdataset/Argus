"""The four derived watchlists, against a real `market_state` projection.

The claim these tests exist to check is narrow and load-bearing: a
derived watchlist has no storage, so it cannot disagree with the state it
is derived from. Everything else about the endpoint — its shape, its
bounds, its refusals — matters less than that.

`UPTREND` (confirmed moves) gets its own section below: it is the one
list that carries phase history and MFE on each entry, and the one list
a security can leave the same way it left any other — by genuinely
changing state.
"""

from __future__ import annotations

from datetime import timedelta

from infra.db.enums import MarketState
from tests.integration.intelligence.conftest import NOW, WINNER


def test_the_four_names_are_the_ones_module_10_defines(client):
    """The set is closed upstream, not restated here."""
    response = client.get("/intelligence/watchlists")

    assert response.status_code == 200
    assert response.json() == ["DOWN_TREND", "CONSOLIDATION", "BREAKOUT_READY", "UPTREND"]


def test_a_state_change_moves_a_security_between_lists_with_no_refresh(client, register, set_state):
    """The guarantee: no cache, so no window where the two disagree.

    The same security is read four times in one test with a state change
    between each read and no refresh, reindex or invalidation step in
    between — because there is nothing to refresh. A cache introduced
    later would fail this at the second read.
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

    set_state(security_id, MarketState.UPTREND)
    assert _ids(client, "BREAKOUT_READY") == []
    assert _ids(client, "UPTREND") == [str(security_id)]


def test_a_state_on_no_list_removes_the_security_from_all_four(client, register, set_state):
    """DISTRIBUTION is not a watchlist state, and nothing here filters it out.

    Absence comes from the state mapping rather than from an exclusion
    this module applies — which is the stronger arrangement, because a
    filter can be forgotten and a missing mapping entry cannot.
    """
    security_id = register("GONE")
    set_state(security_id, MarketState.BREAKOUT_READY)
    assert _ids(client, "BREAKOUT_READY") == [str(security_id)]

    set_state(security_id, MarketState.DISTRIBUTION)

    for name in ("DOWN_TREND", "CONSOLIDATION", "BREAKOUT_READY", "UPTREND"):
        assert _ids(client, name) == []


def test_each_list_covers_exactly_the_states_module_10_maps_to_it(client, register, set_state):
    """Both states in a pair land on the same list, and only that list."""
    pairs = {
        "DOWN_TREND": (MarketState.DOWN_TREND, MarketState.BASE_FORMING),
        "CONSOLIDATION": (MarketState.CONSOLIDATION, MarketState.ACCUMULATION),
        "BREAKOUT_READY": (MarketState.BREAKOUT_WATCH, MarketState.BREAKOUT_READY),
        "UPTREND": (MarketState.UPTREND,),
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


def test_an_unknown_watchlist_names_the_four_that_exist(client):
    """A 404 that tells the caller what they could have asked for."""
    response = client.get("/intelligence/watchlists/MOMENTUM")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "UNKNOWN_WATCHLIST"
    assert error["detail"]["available"] == [
        "DOWN_TREND",
        "CONSOLIDATION",
        "BREAKOUT_READY",
        "UPTREND",
    ]


def test_an_empty_watchlist_is_an_empty_list_and_not_an_error(client):
    """Nothing consolidating is an answer, and a common one."""
    response = client.get("/intelligence/watchlists/CONSOLIDATION")

    assert response.status_code == 200
    payload = response.json()
    assert payload["entries"] == []
    assert payload["count"] == 0
    assert payload["freshness"]["stale"] is False


# --------------------------------------------------------------------------
# UPTREND (confirmed moves) — the one watchlist with lineage attached
# --------------------------------------------------------------------------


def test_the_other_three_watchlists_carry_no_phase_history_or_mfe(
    client, register, set_state, score
):
    """`None`, not `[]` — this list does not show it, rather than "no history"."""
    security_id = register("PLAIN")
    set_state(security_id, MarketState.CONSOLIDATION)
    score(security_id)

    entry = client.get("/intelligence/watchlists/CONSOLIDATION").json()["entries"][0]

    assert entry["phase_history"] is None
    assert entry["mfe"] is None


def test_a_confirmed_moves_entry_carries_its_phase_history_oldest_first(
    client, register, set_state, add_transition
):
    """The lineage that made this security worth showing at all."""
    security_id = register("CONFIRMED")

    down = NOW - timedelta(days=90)
    base = NOW - timedelta(days=60)
    ready = NOW - timedelta(days=20)
    confirmed = NOW - timedelta(days=1)

    add_transition(security_id, to_state=MarketState.DOWN_TREND, at=down)
    add_transition(
        security_id,
        to_state=MarketState.CONSOLIDATION,
        from_state=MarketState.DOWN_TREND,
        at=base,
    )
    add_transition(
        security_id,
        to_state=MarketState.BREAKOUT_READY,
        from_state=MarketState.CONSOLIDATION,
        at=ready,
    )
    add_transition(
        security_id,
        to_state=MarketState.UPTREND,
        from_state=MarketState.BREAKOUT_READY,
        at=confirmed,
    )
    set_state(security_id, MarketState.UPTREND, entered_at=confirmed)

    entry = client.get("/intelligence/watchlists/UPTREND").json()["entries"][0]
    history = entry["phase_history"]

    assert [step["to_state"] for step in history] == [
        "DOWN_TREND",
        "CONSOLIDATION",
        "BREAKOUT_READY",
        "UPTREND",
    ]
    assert history[0]["from_state"] is None
    assert history[-1]["from_state"] == "BREAKOUT_READY"
    assert history[-1]["to_state"] == "UPTREND"
    # Oldest first, matching Module 10's own `history()` ordering.
    times = [step["transition_time"] for step in history]
    assert times == sorted(times)


def test_confirmed_moves_mfe_is_none_before_the_setup_concludes(
    client, register, set_state, concluded_setup
):
    """No live excursion tracking exists. Absence is reported, not invented."""
    security_id = register("STILLOPEN")
    concluded_setup(security_id, WINNER)  # opened and driven to a terminal event,
    #                                       but not yet run through Module 15's
    #                                       `record_outcome` — the `conclude`
    #                                       fixture is deliberately not called.
    set_state(security_id, MarketState.UPTREND)

    entry = client.get("/intelligence/watchlists/UPTREND").json()["entries"][0]

    assert entry["mfe"] is None


def test_confirmed_moves_mfe_is_populated_once_the_setup_concludes(
    client, register, set_state, concluded_setup, conclude
):
    """Once Module 15 has measured it, this list shows the real number."""
    security_id = register("MEASURED")
    setup_id = concluded_setup(security_id, WINNER)
    conclude(setup_id)
    set_state(security_id, MarketState.UPTREND)

    entry = client.get("/intelligence/watchlists/UPTREND").json()["entries"][0]

    assert entry["mfe"] is not None
    assert entry["mfe"] > 0  # WINNER rises throughout; a real, positive excursion.


def test_a_reversal_out_of_uptrend_leaves_the_confirmed_moves_watchlist(
    client, register, set_state, add_transition
):
    """Live view, not a hall of fame — a genuine reversal removes it.

    The security's phase history stays fully queryable through Module
    10's transition log regardless of current watchlist membership; this
    test proves the removal, and the history-preservation half is the
    same guarantee `core/market_state/transitions.py`'s own tests hold.
    """
    security_id = register("REVERSED")
    confirmed_at = NOW - timedelta(days=10)
    reversed_at = NOW - timedelta(days=1)

    add_transition(security_id, to_state=MarketState.UPTREND, at=confirmed_at)
    set_state(security_id, MarketState.UPTREND, entered_at=confirmed_at)
    assert _ids(client, "UPTREND") == [str(security_id)]

    add_transition(
        security_id,
        to_state=MarketState.DOWN_TREND,
        from_state=MarketState.UPTREND,
        at=reversed_at,
        backward=True,
    )
    set_state(security_id, MarketState.DOWN_TREND, entered_at=reversed_at)

    assert _ids(client, "UPTREND") == []
    assert _ids(client, "DOWN_TREND") == [str(security_id)]


def _ids(client, name: str) -> list[str]:
    response = client.get(f"/intelligence/watchlists/{name}")
    assert response.status_code == 200, response.text
    return [entry["security_id"] for entry in response.json()["entries"]]
