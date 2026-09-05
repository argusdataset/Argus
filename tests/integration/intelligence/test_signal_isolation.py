"""The mandatory guarantee from Modules 28 and 29, proved end to end.

All four annotation signals — news volume, SEC 8-K, insider clusters and
13F ownership trend — must have zero influence on watchlist membership or
scoring, not even as a condition. A security that is `BREAKOUT_READY`
purely from price/volume structure has to appear and be evaluated exactly
as before, whether or not any of them exists.

`tests/unit/news_signals/test_isolation_from_scoring_and_market_state.py`
proves this structurally, on the parse tree: none of `core/market_state/`,
`core/scoring/`, `core/candidate_detection/eligibility/` or
`core/live_scanner/` imports either signal package, reaches its schema
module, or names one of its tables as a string.

What that cannot catch is a mistake made from the *other* direction —
someone wiring `insider_cluster_raised` into a filter or a sort inside
`services/intelligence/` itself, where the import boundary does not apply
because reading these signals is exactly what that module is for. This
file closes that gap the only way it can be closed: by writing the
strongest possible signal a security could carry and asserting the served
responses do not move.

The security used here has **no news, no filing, no insider transaction
and no 13F row, ever** — the strongest version of the claim. A security
with real history might coincidentally change for an unrelated reason;
one with none at all leaves no room for that explanation if the watchlist
or the score moves.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from core.news_signals.batch import store_signals
from core.news_signals.filings import SecFilingSignal, store_filing_signals
from core.news_signals.signal import NewsVolumeSignal
from core.ownership_signals.insider import InsiderClusterSignal, store_insider_signals
from core.ownership_signals.institutional import (
    InstitutionalTrend,
    OwnershipQuarter,
    store_institutional_signals,
)
from infra.db.enums import MarketState
from tests.integration.intelligence.conftest import NOW

#: Every field the four signals contribute to the two responses. Excluded
#: from the byte-identical comparison, because these are the fields that
#: are *supposed* to change; everything else is not.
ENTRY_SIGNAL_FIELDS = ("news_signal_raised", "sec_filing_raised", "insider_cluster_raised")
DETAIL_SIGNAL_BLOCKS = (
    "news_signal",
    "sec_filing_signal",
    "insider_cluster",
    "institutional_ownership",
)


def _attach_every_signal(connection, security_id) -> None:
    """Write the loudest reading each of the four signals can produce.

    Deliberately not computed from real data: these are hand-built at
    their most extreme, because the question is not "does the computation
    work" (that is tested elsewhere) but "does *any* value of these
    fields move anything else". A raised 8-K, a six-person insider
    cluster and a 40% ownership jump are what would move something if
    anything could.
    """
    scan_date = NOW.date()

    store_signals(
        connection,
        [
            NewsVolumeSignal(
                security_id=security_id,
                scan_date=scan_date,
                as_of=NOW,
                raised=True,
                today_count=40,
                baseline_mean=1.0,
                baseline_window_days=30,
                multiple_threshold=3.0,
                config_version_label="test-isolation",
                detail={"reason": "test fixture: not a real assessment"},
            )
        ],
    )
    store_filing_signals(
        connection,
        [
            SecFilingSignal(
                security_id=security_id,
                scan_date=scan_date,
                as_of=NOW,
                raised=True,
                item_numbers=("5.02", "1.01"),
                config_version_label="test-isolation",
                detail={"reason": "test fixture: not a real assessment"},
            )
        ],
    )
    store_insider_signals(
        connection,
        [
            InsiderClusterSignal(
                security_id=security_id,
                scan_date=scan_date,
                as_of=NOW,
                raised=True,
                distinct_buyers=6,
                window_days=30,
                min_buyers=2,
                config_version_label="test-isolation",
                detail={"reason": "test fixture: not a real assessment"},
            )
        ],
    )
    store_institutional_signals(
        connection,
        [
            InstitutionalTrend(
                security_id=security_id,
                period=OwnershipQuarter(2026, 2),
                as_of=NOW,
                investors_holding=420,
                investors_holding_change=140,
                total_shares=Decimal("9000000"),
                total_shares_change_percent=Decimal("40.0"),
                ownership_percent=Decimal("71.5"),
                prior_period=OwnershipQuarter(2026, 1),
                config_version_label="test-isolation",
                detail={"reason": "test fixture: not a real assessment"},
            )
        ],
    )


def test_a_security_with_no_signal_data_is_unaffected_by_every_signal_at_once(
    connection, client, register, set_state, score
):
    """The end-to-end proof: snapshot both responses, write all four
    signals at their loudest, and assert nothing moved except the fields
    that exist to move."""
    security_id = register("NOSIGNALS")
    set_state(security_id, MarketState.BREAKOUT_READY, entered_at=NOW - timedelta(days=2))
    signal_id = score(security_id, strong=True)
    assert signal_id is not None, "a strong candidate must produce a real score"

    before_entry = _entry_for(client, "BREAKOUT_READY", security_id)
    before_detail = client.get("/intelligence/securities/NOSIGNALS").json()

    # Nothing computed yet: every signal field is absent.
    assert all(before_entry[field] is None for field in ENTRY_SIGNAL_FIELDS)
    assert before_detail["sec_filing_signal"]["raised"] is None
    assert before_detail["insider_cluster"]["raised"] is None
    assert before_detail["institutional_ownership"]["year"] is None

    _attach_every_signal(connection, security_id)

    after_entry = _entry_for(client, "BREAKOUT_READY", security_id)
    after_detail = client.get("/intelligence/securities/NOSIGNALS").json()

    # The fields that are allowed, and expected, to change.
    assert after_entry["news_signal_raised"] is True
    assert after_entry["sec_filing_raised"] is True
    assert after_entry["insider_cluster_raised"] is True
    assert after_detail["sec_filing_signal"]["item_numbers"] == ["5.02", "1.01"]
    assert after_detail["insider_cluster"]["distinct_buyers"] == 6
    assert after_detail["institutional_ownership"]["total_shares_change_percent"] == 40.0

    # Everything else: byte-identical. Membership, state, score, and every
    # other block on the detail response. `freshness` is excluded from the
    # detail comparison only because it is stamped from the wall clock at
    # request time and would differ by microseconds regardless of this
    # test — nothing about these signals touches it.
    assert _without(before_entry, *ENTRY_SIGNAL_FIELDS) == _without(
        after_entry, *ENTRY_SIGNAL_FIELDS
    )
    assert _without(before_detail, *DETAIL_SIGNAL_BLOCKS, "freshness") == _without(
        after_detail, *DETAIL_SIGNAL_BLOCKS, "freshness"
    )


def test_the_score_block_is_identical_down_to_every_component(
    connection, client, register, set_state, score
):
    """Stated separately because it is the claim that matters most.

    The scoring boundary is the one these signals could do real damage
    through: a component value nudged by a raised flag would be invisible
    in a total and would change what ARGUS ranks. So the whole score
    block, components included, is compared rather than just the headline
    number.
    """
    security_id = register("SCOREFIXED")
    set_state(security_id, MarketState.BREAKOUT_READY)
    assert score(security_id, strong=True) is not None

    before = client.get("/intelligence/securities/SCOREFIXED").json()["score"]
    _attach_every_signal(connection, security_id)
    after = client.get("/intelligence/securities/SCOREFIXED").json()["score"]

    assert before == after
    assert before["components"] == after["components"]


def test_no_signal_can_add_or_remove_a_security_from_a_watchlist(
    connection, client, register, set_state
):
    """The membership half, isolated from scoring: a security with no
    score at all appears on `BREAKOUT_READY` purely by `market_state`,
    before and after every signal is attached — and a security with the
    loudest possible signals but no such state never appears at all."""
    member = register("STAYS")
    set_state(member, MarketState.BREAKOUT_READY)
    assert _ids(client, "BREAKOUT_READY") == [str(member)]

    _attach_every_signal(connection, member)
    assert _ids(client, "BREAKOUT_READY") == [str(member)]

    # The converse: signals cannot admit a security to a list its state
    # does not put it on.
    outsider = register("NEVERLISTED")
    set_state(outsider, MarketState.DOWN_TREND)
    _attach_every_signal(connection, outsider)

    assert str(outsider) not in _ids(client, "BREAKOUT_READY")
    assert str(outsider) in _ids(client, "DOWN_TREND")


def test_the_watchlist_order_does_not_change_when_signals_are_attached(
    connection, client, register, set_state
):
    """A subtler failure than membership: a signal used as a sort key
    would leave every security present and still change what a person
    sees first, which is most of what a list is for."""
    identities = []
    for index in range(4):
        security_id = register(f"ORDER{index}")
        set_state(security_id, MarketState.CONSOLIDATION, entered_at=NOW - timedelta(days=index))
        identities.append(security_id)

    before = _ids(client, "CONSOLIDATION")

    # Loudest possible signals on exactly one of them — the one a
    # signal-aware sort would push to the top.
    _attach_every_signal(connection, identities[-1])

    assert _ids(client, "CONSOLIDATION") == before


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
    return {key: value for key, value in payload.items() if key not in keys}
