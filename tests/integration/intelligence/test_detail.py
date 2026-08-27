"""The per-asset detail: the assembled answer, including the refusals.

Two things are being checked here that the rest of the module cannot
check for itself. First, that an `INSUFFICIENT_EVIDENCE` candidate — the
common case today — gets a complete response rather than an error or a
hollowed-out one. Second, that cross-asset and same-asset similarity
arrive as two objects and never as one.
"""

from __future__ import annotations

from datetime import timedelta

from infra.db.enums import AnalogueScope, MarketState
from tests.integration.intelligence.conftest import NOW

#: Every block the detail response promises. A refusal must fill all of
#: them, which is what makes it a complete answer rather than a stub.
BLOCKS = (
    "security_id",
    "ticker",
    "name",
    "state",
    "score",
    "similarity",
    "risk",
    "explanation",
    "freshness",
)


# --------------------------------------------------------------------------
# INSUFFICIENT_EVIDENCE is a complete answer
# --------------------------------------------------------------------------


def test_an_insufficient_evidence_candidate_returns_200_with_every_block(
    client, register, set_state, score
):
    """The response ARGUS gives most often today, and it is a full one.

    Module 13 refuses to score a candidate whose evidence component is
    unmeasurable, and today that is nearly all of them. If this endpoint
    treated the refusal as a failure, the API would be broken for the
    normal case and working for the exception.
    """
    security_id = register("REFUSED")
    set_state(security_id, MarketState.CONSOLIDATION)
    signal_id = score(security_id, strong=False)
    assert signal_id is not None, "Module 13 stores the refusal as a signal row"

    response = client.get("/intelligence/securities/REFUSED")

    assert response.status_code == 200
    payload = response.json()
    assert set(BLOCKS) <= set(payload)
    assert all(payload[block] is not None for block in BLOCKS)


def test_the_refusal_names_the_decision_rather_than_a_generic_failure(
    client, register, set_state, score
):
    """`INSUFFICIENT_EVIDENCE` is on the wire, not "unavailable".

    A client showing different copy for a candidate ARGUS could not
    measure and one it excluded before scoring is doing the right thing,
    and it can only do it if the two look different here.
    """
    security_id = register("NAMED")
    set_state(security_id, MarketState.CONSOLIDATION)
    score(security_id, strong=False)

    block = client.get("/intelligence/securities/NAMED").json()["score"]

    assert block["scored"] is False
    assert block["unavailable"]["reason"] == "INSUFFICIENT_EVIDENCE"
    assert "declined to score" in block["unavailable"]["explanation"]
    assert block["argus_score"] is None
    assert block["confidence"] is None


def test_the_refusal_still_carries_components_coverage_and_provenance(
    client, register, set_state, score
):
    """The evidence behind the refusal, not just the word.

    A refusal with no components would be ARGUS saying "no" and declining
    to say why — which is the failure mode every module in this project
    has been built to avoid.
    """
    security_id = register("WHYNOT")
    set_state(security_id, MarketState.CONSOLIDATION)
    score(security_id, strong=False)

    block = client.get("/intelligence/securities/WHYNOT").json()["score"]

    assert block["components"], "the seven components are reported for a refusal too"
    assert block["weight_coverage"] is not None
    assert block["provenance"]["scoring_configuration_id"]
    assert block["provenance"]["signal_id"]


def test_the_refusal_carries_module_16s_explanation_and_not_an_empty_block(
    client, register, set_state, score
):
    """Module 16 narrates the refusal; this module passes it through.

    `explain_signal` dispatches to the refusal narrator when the decision
    is not SCORED, so the caller gets one shape either way. A branch here
    that skipped narration for unscored signals would leave the common
    case with nothing to read.
    """
    security_id = register("NARRATED")
    set_state(security_id, MarketState.CONSOLIDATION)
    score(security_id, strong=False)

    block = client.get("/intelligence/securities/NARRATED").json()["explanation"]

    assert block["unavailable"] is None
    assert block["kind"]
    assert block["headline"]["text"]
    assert block["headline"]["cites"], "every claim names the facts it rests on"
    assert block["sections"]
    assert block["text"]


def test_a_scored_candidate_and_a_refused_one_have_the_same_response_shape(
    client, register, set_state, score
):
    """One layout, not two. The refusal is not a different endpoint."""
    scored_id = register("SCORED")
    set_state(scored_id, MarketState.CONSOLIDATION)
    score(scored_id, strong=True)

    refused_id = register("NOTSCORED")
    set_state(refused_id, MarketState.CONSOLIDATION)
    score(refused_id, strong=False)

    scored = client.get("/intelligence/securities/SCORED").json()
    refused = client.get("/intelligence/securities/NOTSCORED").json()

    assert set(scored) == set(refused)
    assert set(scored["score"]) == set(refused["score"])
    assert scored["score"]["scored"] is True
    assert refused["score"]["scored"] is False


def test_a_security_ARGUS_has_never_scored_is_also_a_complete_answer(client, register, set_state):
    """No signal at all is a third thing, and it says so.

    Distinct from a refusal: nothing has looked at this security. A client
    that showed "insufficient evidence" here would be reporting a
    judgement ARGUS never made.
    """
    set_state(register("UNSEEN"), MarketState.DOWN_TREND)

    payload = client.get("/intelligence/securities/UNSEEN").json()

    assert payload["score"]["scored"] is False
    assert payload["score"]["unavailable"]["reason"] == "no_signal"
    assert payload["similarity"]["unavailable"]["reason"] == "no_similarity_evidence"
    assert payload["risk"]["unavailable"]["reason"] == "no_risk_assessment"
    assert payload["explanation"]["unavailable"]["reason"] == "no_explanation"
    assert payload["state"]["state"] == "DOWN_TREND"


# --------------------------------------------------------------------------
# Cross-asset and same-asset never merge
# --------------------------------------------------------------------------


def test_the_two_similarity_scopes_arrive_as_two_objects(
    client, register, set_state, add_similarity
):
    """Module 11 stores them as separate rows so they cannot be blended.

    This is the last place that refusal could quietly be undone — one
    weighted average here and "similar securities did this" would become
    indistinguishable from "this security did this".
    """
    security_id = register("BOTH")
    set_state(security_id, MarketState.CONSOLIDATION)
    add_similarity(
        security_id,
        scope=AnalogueScope.CROSS_ASSET,
        match_count=180,
        sufficiency="ADEQUATE",
        median_outcome=0.21,
        failure_rate=0.31,
    )
    add_similarity(
        security_id,
        scope=AnalogueScope.SAME_ASSET,
        match_count=40,
        sufficiency="ADEQUATE",
        median_outcome=0.09,
        failure_rate=0.55,
    )

    block = client.get("/intelligence/securities/BOTH").json()["similarity"]

    assert block["cross_asset"]["scope"] == "CROSS_ASSET"
    assert block["same_asset"]["scope"] == "SAME_ASSET"
    assert block["cross_asset"]["match_count"] == 180
    assert block["same_asset"]["match_count"] == 40
    assert block["cross_asset"]["median_outcome"] == 0.21
    assert block["same_asset"]["median_outcome"] == 0.09

    # And no combined figure exists to be mistaken for either one.
    assert "combined" not in block
    assert "blended" not in block
    assert 220 not in [
        block["cross_asset"]["match_count"],
        block["same_asset"]["match_count"],
    ]


def test_one_scope_being_adequate_does_not_lend_its_sufficiency_to_the_other(
    client, register, set_state, add_similarity
):
    """Each scope carries its own sufficiency and its own suppression.

    A hundred cross-asset analogues say nothing about how often this
    security has done it before. Reporting the same-asset rate because the
    cross-asset sample was healthy would be the blend, arrived at sideways.
    """
    security_id = register("LOPSIDED")
    set_state(security_id, MarketState.CONSOLIDATION)
    add_similarity(
        security_id,
        scope=AnalogueScope.CROSS_ASSET,
        match_count=200,
        sufficiency="ADEQUATE",
        failure_rate=0.28,
    )
    add_similarity(
        security_id,
        scope=AnalogueScope.SAME_ASSET,
        match_count=2,
        sufficiency="INSUFFICIENT",
        failure_rate=0.50,
    )

    block = client.get("/intelligence/securities/LOPSIDED").json()["similarity"]

    assert block["cross_asset"]["sufficiency"] == "ADEQUATE"
    assert block["cross_asset"]["failure_rate"] == 0.28

    same = block["same_asset"]
    assert same["sufficiency"] == "INSUFFICIENT"
    assert same["match_count"] == 2, "the count is reported"
    assert same["failure_rate"] is None, "the rate from two cases is not"
    assert same["unavailable"]["reason"] == "INSUFFICIENT"
    assert same["unavailable"]["observed"] == 2


def test_a_security_with_only_one_scope_leaves_the_other_null(
    client, register, set_state, add_similarity
):
    """Absent is null, never a zero-filled object."""
    security_id = register("ONESIDE")
    set_state(security_id, MarketState.CONSOLIDATION)
    add_similarity(
        security_id,
        scope=AnalogueScope.CROSS_ASSET,
        match_count=90,
        sufficiency="SPARSE",
    )

    block = client.get("/intelligence/securities/ONESIDE").json()["similarity"]

    assert block["cross_asset"] is not None
    assert block["same_asset"] is None


def test_the_newest_result_per_scope_wins_without_crossing_scopes(
    client, register, set_state, add_similarity
):
    """A fresh cross-asset run does not stale-out the same-asset one."""
    security_id = register("NEWEST")
    set_state(security_id, MarketState.CONSOLIDATION)
    add_similarity(
        security_id,
        scope=AnalogueScope.SAME_ASSET,
        match_count=11,
        sufficiency="SPARSE",
        at=NOW - timedelta(days=30),
    )
    add_similarity(
        security_id,
        scope=AnalogueScope.CROSS_ASSET,
        match_count=50,
        sufficiency="SPARSE",
        at=NOW - timedelta(days=20),
    )
    add_similarity(
        security_id,
        scope=AnalogueScope.CROSS_ASSET,
        match_count=140,
        sufficiency="ADEQUATE",
        at=NOW - timedelta(days=2),
    )

    block = client.get("/intelligence/securities/NEWEST").json()["similarity"]

    assert block["cross_asset"]["match_count"] == 140
    assert block["same_asset"]["match_count"] == 11


# --------------------------------------------------------------------------
# What is served, and what is not
# --------------------------------------------------------------------------


def test_the_state_block_names_the_lists_that_state_puts_it_on(client, register, set_state):
    """Read from Module 10's mapping, not restated in this service."""
    set_state(register("ONLIST"), MarketState.ACCUMULATION)

    block = client.get("/intelligence/securities/ONLIST").json()["state"]

    assert block["state"] == "ACCUMULATION"
    assert block["watchlists"] == ["CONSOLIDATION"]


def test_a_state_on_no_list_reports_an_empty_watchlist_membership(client, register, set_state):
    """UPTREND is a state ARGUS holds and a list it is on none of."""
    set_state(register("OFFLIST"), MarketState.UPTREND)

    block = client.get("/intelligence/securities/OFFLIST").json()["state"]

    assert block["state"] == "UPTREND"
    assert block["watchlists"] == []


def test_no_detail_response_carries_a_fundamentals_field(client, register, set_state, score):
    """Revenue and P/E belong to the Terminal. Checked on the wire.

    The structural test asserts nothing here can reach a fundamentals
    read; this asserts the served JSON has no such key, which is what a
    consumer would actually see.
    """
    security_id = register("NOFUND")
    set_state(security_id, MarketState.CONSOLIDATION)
    score(security_id)

    body = client.get("/intelligence/securities/NOFUND").text.lower()

    for term in ("revenue", "eps", "p_e", "pe_ratio", "market_cap", "earnings_per_share"):
        assert term not in body


def test_an_unknown_ticker_is_a_404_naming_the_ticker(client):
    """Not a 500, and not an empty detail for a security that isn't there."""
    response = client.get("/intelligence/securities/NOSUCH")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "SECURITY_NOT_FOUND"
    assert error["detail"]["ticker"] == "NOSUCH"


def test_the_served_numbers_are_the_stored_numbers(client, register, set_state, score, connection):
    """The reshaping in `reads.py` is a shape change, not an arithmetic one."""
    from sqlalchemy import select

    from infra.db.schema.intelligence import signals

    security_id = register("SAMEVAL")
    set_state(security_id, MarketState.CONSOLIDATION)
    score(security_id, strong=True)

    row = connection.execute(select(signals).where(signals.c.security_id == security_id)).one()
    block = client.get("/intelligence/securities/SAMEVAL").json()["score"]

    assert block["argus_score"] == float(row.argus_score)
    assert block["confidence"] == float(row.confidence)
    assert block["opportunity_score"] == float(row.opportunity_score)
    assert block["risk_score"] == float(row.risk_score)
    assert block["probability"] is None, "Module 13 stores no probability, and none is invented"


# --------------------------------------------------------------------------
# What is served is what ARGUS currently stands behind
# --------------------------------------------------------------------------


def test_a_withdrawn_score_is_not_served_as_the_current_one(
    client, register, set_state, score, supersede
):
    """A correction replaces the original; the original stops being an answer.

    `signals` is append-only, so a withdrawn score is still in the table
    and still perfectly readable. That is exactly the hazard: a reader
    that forgot `supersedes_signal_id IS NULL` would find the old row,
    serve it, and show a number ARGUS has already retracted — with full
    provenance attached, which would make it look more trustworthy rather
    than less.
    """
    security_id = register("WITHDRAWN")
    set_state(security_id, MarketState.CONSOLIDATION)
    original_id = score(security_id, strong=True, at=NOW - timedelta(days=2))
    assert original_id is not None
    correction_id = supersede(security_id, original_id, at=NOW - timedelta(hours=1))

    block = client.get("/intelligence/securities/WITHDRAWN").json()["score"]

    assert block["provenance"]["signal_id"] == str(correction_id)
    assert block["provenance"]["signal_id"] != str(original_id)
    assert block["scored"] is False, "the correction declined to score; the original had"
    assert block["argus_score"] is None


def test_an_event_announced_later_is_not_in_todays_answer(
    client, register, set_state, score, schedule_event
):
    """The PIT filter, at the last place a number leaves the database.

    Every canonical read in ARGUS filters on `availability_time`, and this
    endpoint is not an exception because it is an API. An earnings date
    ARGUS learns tomorrow appearing in today's risk block would be
    foreknowledge of a schedule — the quiet kind of leak that produces a
    plausible number rather than an error.
    """
    security_id = register("ANNOUNCED")
    set_state(security_id, MarketState.CONSOLIDATION)
    score(security_id)
    schedule_event(
        security_id,
        scheduled_for=NOW + timedelta(days=10),
        announced_at=NOW - timedelta(days=3),
    )
    schedule_event(
        security_id,
        scheduled_for=NOW + timedelta(days=20),
        announced_at=NOW + timedelta(days=5),
        event_type="TRIAL_RESULT",
    )

    events = client.get("/intelligence/securities/ANNOUNCED").json()["risk"]["pending_events"]

    assert [event["event_type"] for event in events] == ["EARNINGS"]


def test_an_event_already_past_is_not_pending(client, register, set_state, score, schedule_event):
    """ "Pending" means ahead of now, not merely known."""
    security_id = register("PASTEVENT")
    set_state(security_id, MarketState.CONSOLIDATION)
    score(security_id)
    schedule_event(
        security_id,
        scheduled_for=NOW - timedelta(days=2),
        announced_at=NOW - timedelta(days=30),
    )

    events = client.get("/intelligence/securities/PASTEVENT").json()["risk"]["pending_events"]

    assert events == []
