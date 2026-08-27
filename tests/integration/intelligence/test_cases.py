"""The "why did this fail" view: Module 15's record, Module 16's words.

This endpoint's whole job is to not add anything. So these tests compare
what it serves against what Modules 15 and 16 produce when called
directly — if a sentence, a label or a number differs, this module wrote
something, and writing something is the one thing it must not do.
"""

from __future__ import annotations

import pytest

from core.explanation.narrators import explain_case
from core.outcome_tracking.engine import compute_case
from infra.db.enums import OutcomeStatus
from services.intelligence.cases import CLASSIFICATION_CAVEAT
from tests.integration.intelligence.conftest import LOSER, NOW, WINNER


@pytest.fixture
def failed_setup(register, concluded_setup, conclude):
    """A setup that fell past the failure threshold, with its outcome stored."""

    def _build(ticker: str = "FELL", closes: list[float] | None = None):
        security_id = register(ticker)
        setup_id = concluded_setup(security_id, closes or LOSER)
        conclude(setup_id)
        return security_id, setup_id

    return _build


def test_the_served_explanation_is_module_16s_output_verbatim(
    client, connection, failed_setup, lineage
):
    """The claim, checked against the source rather than eyeballed.

    Module 16 built a fabrication guard around this text: every sentence
    cites a fact from the input and a verifier rejects one that does not.
    Paraphrasing here would step outside that guard while looking
    harmless, so the served text is compared to the narrator's own output
    claim by claim.
    """
    _security_id, setup_id = failed_setup()

    served = client.get(f"/intelligence/setups/{setup_id}/outcome").json()
    expected = explain_case(
        compute_case(
            connection,
            setup_id,
            as_of=NOW,
            data_snapshot_id=lineage.data_snapshot_id,
        ).as_dict()
    ).as_dict()

    assert served["explanation"]["kind"] == expected["kind"]
    assert served["explanation"]["headline"] == expected["headline"]
    assert served["explanation"]["sections"] == expected["sections"]
    assert served["explanation"]["text"] == expected["text"]
    assert served["explanation"]["omissions"] == expected["omissions"]


def test_the_fact_registry_travels_with_the_text(client, failed_setup):
    """A reader can check a sentence without asking anything else.

    Module 16 attaches the registry the explanation was built from for
    exactly this reason. Dropping it at the API boundary would leave the
    prose intact and the checkability gone.
    """
    _security_id, setup_id = failed_setup()

    explanation = client.get(f"/intelligence/setups/{setup_id}/outcome").json()["explanation"]

    assert explanation["facts"]
    cited = set(explanation["headline"]["cites"])
    for section in explanation["sections"]:
        for claim in section["claims"]:
            cited |= set(claim["cites"])
    assert cited <= set(explanation["facts"]), "every citation resolves in the registry"


def test_a_failure_and_a_success_are_served_through_the_identical_path(client, failed_setup):
    """Module 15 made failures as complete as successes; this keeps them so.

    A view that narrated wins richly and losses thinly would teach every
    reader — and eventually every model trained on the archive — that
    wins are more knowable, which is exactly backwards.
    """
    _lost_id, lost_setup = failed_setup("LOST", LOSER)
    _won_id, won_setup = failed_setup("WON", WINNER)

    lost = client.get(f"/intelligence/setups/{lost_setup}/outcome").json()
    won = client.get(f"/intelligence/setups/{won_setup}/outcome").json()

    assert lost["outcome_status"] == OutcomeStatus.FAILED.value
    assert won["outcome_status"] == OutcomeStatus.SUCCESS.value
    assert set(lost) == set(won)
    assert set(lost["explanation"]) == set(won["explanation"])
    assert lost["explanation"]["text"]
    assert len(lost["explanation"]["sections"]) == len(won["explanation"]["sections"])


def test_the_classification_caveat_travels_beside_the_label(client, failed_setup):
    """Module 15 called its thresholds unvalidated; the response says so.

    A false-positive type without its stated limits reads as a finding.
    With them it reads as what it is — a judgement about measurements,
    printed next to the measurements.
    """
    _security_id, setup_id = failed_setup()

    payload = client.get(f"/intelligence/setups/{setup_id}/outcome").json()

    assert payload["classification_caveat"] == CLASSIFICATION_CAVEAT
    assert "unvalidated" in payload["classification_caveat"]


def test_the_served_labels_are_the_stored_ones(client, connection, failed_setup):
    """Read from `setup_outcomes`, not re-derived at request time."""
    from sqlalchemy import select

    from infra.db.schema.setups import setup_outcomes

    security_id, setup_id = failed_setup()

    row = connection.execute(
        select(setup_outcomes).where(setup_outcomes.c.setup_id == setup_id)
    ).one()
    payload = client.get(f"/intelligence/setups/{setup_id}/outcome").json()

    assert payload["security_id"] == str(security_id)
    assert payload["outcome_status"] == str(row.outcome_status)
    assert payload["false_positive_type"] == (
        str(row.false_positive_type) if row.false_positive_type else None
    )
    assert payload["review_confidence"] == (
        str(row.review_confidence) if row.review_confidence else None
    )


def test_an_unconcluded_setup_is_a_409_and_not_a_404(client, register, connection, lineage):
    """The setup exists and is healthy; it simply has no outcome yet.

    Telling a caller it does not exist would be wrong in a way they could
    not diagnose — they would go looking for a missing row rather than
    waiting for a horizon to close.
    """
    from core.lifecycle.engine import open_setup
    from tests.integration.intelligence.conftest import DETECTED  # noqa: PLC0415

    security_id = register("PENDING")
    setup_id, _ = open_setup(connection, security_id, as_of=DETECTED, lineage=lineage)

    response = client.get(f"/intelligence/setups/{setup_id}/outcome")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "NOT_CONCLUDED"
    assert "still tracking" in error["message"].lower()


def test_an_unknown_setup_is_a_404(client):
    """A setup id nobody wrote is a genuine absence."""
    response = client.get("/intelligence/setups/00000000-0000-0000-0000-000000000000/outcome")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SETUP_NOT_FOUND"
