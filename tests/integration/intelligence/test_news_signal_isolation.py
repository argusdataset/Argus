"""The mandatory guarantee from Module 28's own brief, proved end to end.

Module 28's news-volume signal must have zero influence on watchlist
membership or scoring — not even as a condition. A security that is
`BREAKOUT_READY` purely from price/volume structure has to appear and be
evaluated exactly as before, whether or not a news-signal row exists for
it at all.

`tests/unit/news_signals/test_isolation_from_scoring_and_market_state.py`
proves this structurally, on the parse tree: none of `core/market_state/`,
`core/scoring/`, `core/candidate_detection/eligibility/` or
`core/live_scanner/` import Module 28. What that test cannot catch is a
future mistake made from the *other* direction — someone wiring
`news_signal_raised` into a filter or a weight inside
`services/intelligence/` itself, where the import boundary does not apply
because this module is exactly where reading the signal is supposed to
happen. This test closes that gap the same way Module 21's own "no
computation" AST test is paired with behavioural tests elsewhere: by
proving the actual response is unchanged, not just the import graph.

The security here has **no `canonical_news` row, ever** — the strongest
version of the claim. A security with real news history might coincidally
change for an unrelated reason; one with none at all leaves no room for
that explanation if the watchlist or the score moves.
"""

from __future__ import annotations

from datetime import timedelta

from core.news_signals.batch import store_signals
from core.news_signals.signal import NewsVolumeSignal
from infra.db.enums import MarketState
from tests.integration.intelligence.conftest import NOW


def _news_signal_row(**overrides):
    """A `NewsVolumeSignal`, deliberately not computed from real counts.

    The strongest possible case for "this would be visible if it leaked
    into scoring": `raised=True` at a high multiple, attached to a
    security that has never had a single article. If this signal reached
    scoring or the watchlist filter through any path, this is the reading
    that would move something.
    """
    defaults: dict = {
        "scan_date": NOW.date(),
        "as_of": NOW,
        "raised": True,
        "today_count": 25,
        "baseline_mean": 1.0,
        "baseline_window_days": 30,
        "multiple_threshold": 3.0,
        "config_version_label": "test-isolation",
        "unavailable": None,
        "detail": {"reason": "test fixture: not a real assessment"},
    }
    defaults.update(overrides)
    return NewsVolumeSignal(security_id=defaults.pop("security_id"), **defaults)


def test_a_zero_news_breakout_ready_security_is_unaffected_by_a_raised_news_signal(
    connection, client, register, set_state, score
):
    """The end-to-end proof: write the news signal *after* capturing the
    watchlist entry and the detail response, then assert neither response
    changed anywhere except the one field that is supposed to."""
    security_id = register("ZERONEWSBR")
    set_state(security_id, MarketState.BREAKOUT_READY, entered_at=NOW - timedelta(days=2))
    signal_id = score(security_id, strong=True)
    assert signal_id is not None, "a strong candidate must produce a real score"

    before_watchlist = _entry_for(client, "BREAKOUT_READY", security_id)
    before_detail = client.get("/intelligence/securities/ZERONEWSBR").json()

    assert before_watchlist["news_signal_raised"] is None
    assert before_detail["news_signal"]["raised"] is None

    # Attach the strongest possible reading directly to the connection
    # this test's own client shares (see the `client` fixture's savepoint
    # wiring) — no news-signal batch run needed for this to be visible.
    written = store_signals(connection, [_news_signal_row(security_id=security_id)])
    assert written == 1

    after_watchlist = _entry_for(client, "BREAKOUT_READY", security_id)
    after_detail = client.get("/intelligence/securities/ZERONEWSBR").json()

    # The one field that is allowed, and expected, to change.
    assert after_watchlist["news_signal_raised"] is True
    assert after_detail["news_signal"]["raised"] is True

    # Everything else: byte-identical. Membership, state, score, and every
    # other block on the detail response. `freshness` is excluded from the
    # detail comparison only because it is stamped from the wall clock at
    # request time (`datetime.now(UTC)` per call) and would differ by
    # microseconds regardless of this test — nothing about Module 28
    # touches it.
    assert _without(before_watchlist, "news_signal_raised") == _without(
        after_watchlist, "news_signal_raised"
    )
    assert _without(before_detail, "news_signal", "freshness") == _without(
        after_detail, "news_signal", "freshness"
    )


def test_a_raised_news_signal_does_not_admit_or_evict_a_security_from_the_watchlist(
    connection, client, register, set_state
):
    """The membership half, isolated from scoring: a security with no
    score at all (never candidate-scored) still appears or not on
    `BREAKOUT_READY` purely by `market_state`, before and after a news
    signal is attached to it."""
    security_id = register("MEMBERSHIP")
    set_state(security_id, MarketState.BREAKOUT_READY)

    assert _ids(client, "BREAKOUT_READY") == [str(security_id)]

    store_signals(connection, [_news_signal_row(security_id=security_id)])
    assert _ids(client, "BREAKOUT_READY") == [str(security_id)]

    # And the converse: a security with a raised news signal but no
    # BREAKOUT_READY state must not appear either — the signal cannot add
    # a security to a watchlist any more than it can remove one.
    other_security_id = register("NOTONLIST")
    store_signals(connection, [_news_signal_row(security_id=other_security_id)])
    assert str(other_security_id) not in _ids(client, "BREAKOUT_READY")


def _entry_for(client, watchlist_name: str, security_id) -> dict:
    payload = client.get(f"/intelligence/watchlists/{watchlist_name}").json()
    matches = [entry for entry in payload["entries"] if entry["security_id"] == str(security_id)]
    assert len(matches) == 1, f"expected exactly one entry for {security_id}, found {matches}"
    return matches[0]


def _ids(client, name: str) -> list[str]:
    response = client.get(f"/intelligence/watchlists/{name}")
    assert response.status_code == 200, response.text
    return [entry["security_id"] for entry in response.json()["entries"]]


def _without(payload: dict, *keys: str) -> dict:
    return {k: v for k, v in payload.items() if k not in keys}
