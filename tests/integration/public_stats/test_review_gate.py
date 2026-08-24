"""Can an unapproved result reach the public page? — asserted behaviourally.

The structural test in `tests/unit/public_stats/` shows the shape holds.
These show the shape does what it claims: seed outcomes, leave the run
unapproved, and confirm nothing about them is reachable through anything
this module exposes.

The strongest test here is the last one — a run approved, published, then
rejected. That is the case where materialization could quietly keep
serving a withdrawn figure, and it is the specific failure the whole
`gate_fingerprint` mechanism exists to prevent.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from services.public_stats.aggregates import CHARTS
from services.public_stats.errors import PublicStatsError
from services.public_stats.gate import current_scope, published_dataset
from services.public_stats.releases import (
    approve_window,
    open_window_for_review,
    reject_window,
)
from services.public_stats.snapshots import refresh_public_stats
from tests.integration.public_stats.conftest import (
    AS_OF,
    LIVE_END,
    LIVE_START,
    PERIOD_START,
)

# --------------------------------------------------------------------------
# Nothing unapproved is reachable
# --------------------------------------------------------------------------


def test_an_unreviewed_run_contributes_nothing(connection, make_run, seed_outcomes):
    """A run that completed and was never queued for review is not a
    result under review — it is a result nobody has looked at."""
    make_run()
    seed_outcomes(40, prefix="UNR")

    scope = current_scope(connection)

    assert scope.is_empty
    assert published_dataset(connection, scope, as_of=AS_OF).frame.empty


def test_a_pending_run_contributes_nothing(connection, make_run, seed_outcomes, reviewer):
    """Queued and awaiting a decision. The state the whole gate exists
    for: results that are finished, correct, and not yet permitted."""
    from core.model_validation_evaluation.validation.review import open_for_review

    run_id = make_run()
    open_for_review(connection, run_id)
    seed_outcomes(40, prefix="PEN")

    scope = current_scope(connection)

    assert scope.is_empty
    assert published_dataset(connection, scope, as_of=AS_OF).frame.empty


def test_a_rejected_run_contributes_nothing(connection, make_run, seed_outcomes, reviewer):
    make_run(status="REJECTED", reviewer_id=reviewer)
    seed_outcomes(40, prefix="REJ")

    scope = current_scope(connection)

    assert scope.is_empty
    assert published_dataset(connection, scope, as_of=AS_OF).frame.empty


def test_an_approved_run_contributes_its_outcomes(connection, make_run, seed_outcomes, reviewer):
    """The positive control. Without it the tests above pass against a
    module that publishes nothing at all, which is not the same as one
    that publishes only approved results."""
    make_run(status="APPROVED", reviewer_id=reviewer)
    seed_outcomes(40, prefix="APP")

    scope = current_scope(connection)
    dataset = published_dataset(connection, scope, as_of=AS_OF)

    assert len(scope.runs) == 1
    assert len(dataset.frame) == 40


def test_approved_and_rejected_runs_together_publish_only_the_approved(
    connection, make_run, seed_outcomes, reviewer
):
    """The mixed case, which is where a filter built from a list goes
    wrong: an empty list means no filter, and a partly-full one means the
    wrong filter."""
    make_run(
        status="APPROVED",
        reviewer_id=reviewer,
        period_start=PERIOD_START,
        period_end=PERIOD_START + timedelta(days=30),
    )
    make_run(
        status="REJECTED",
        reviewer_id=reviewer,
        period_start=PERIOD_START + timedelta(days=60),
        period_end=PERIOD_START + timedelta(days=90),
    )
    seed_outcomes(10, prefix="OK", detected_from=PERIOD_START)
    seed_outcomes(10, prefix="NO", detected_from=PERIOD_START + timedelta(days=61))

    dataset = published_dataset(connection, current_scope(connection), as_of=AS_OF)

    assert len(dataset.frame) == 10


def test_a_run_approved_then_rejected_is_no_longer_in_scope(
    connection, make_run, seed_outcomes, reviewer
):
    """Current status, not "has an APPROVED row" — Module 17's rule,
    inherited rather than re-implemented."""
    from core.model_validation_evaluation.validation.review import reject

    run_id = make_run(status="APPROVED", reviewer_id=reviewer)
    seed_outcomes(40, prefix="FLIP")
    assert not current_scope(connection).is_empty

    reject(connection, run_id, user_id=reviewer, note="withdrawn")

    assert current_scope(connection).is_empty


@pytest.mark.parametrize("chart", CHARTS)
def test_no_endpoint_serves_anything_when_nothing_is_approved(
    client, make_run, seed_outcomes, chart
):
    """Every endpoint, not a representative one. The requirement is
    absolute, so the test is exhaustive."""
    make_run()
    seed_outcomes(40, prefix="NONE")

    response = client.get(f"/public/stats/{chart}")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STATISTICS_UNAVAILABLE"


def test_the_summary_reports_nothing_published_rather_than_zero(client, make_run, seed_outcomes):
    """ "We have not published statistics" and "our statistics are zero"
    are different facts, and only one of them is true."""
    make_run()
    seed_outcomes(40, prefix="SUM")

    payload = client.get("/public/stats").json()

    assert payload["published"] is False
    assert payload["approved_run_count"] == 0
    assert "not a result of zero" in payload["explanation"]


# --------------------------------------------------------------------------
# Withdrawal after publication — the case materialization could get wrong
# --------------------------------------------------------------------------


def test_a_rejected_run_stops_being_served_even_though_it_was_materialized(
    connection, client, make_run, seed_outcomes, reviewer
):
    """The failure the fingerprint exists to prevent.

    Approve, publish, reject. The stored payload still contains the
    rejected run's numbers. Serving it would be exactly the quiet
    inflation this module is built not to do.
    """
    from core.model_validation_evaluation.validation.review import reject

    run_id = make_run(status="APPROVED", reviewer_id=reviewer)
    seed_outcomes(40, prefix="WDR")
    refresh_public_stats(connection, as_of=AS_OF)

    served = client.get("/public/stats/win_rate")
    assert served.status_code == 200
    assert served.json()["sample_size"] == 40

    reject(connection, run_id, user_id=reviewer, note="found a problem")

    withdrawn = client.get("/public/stats/win_rate")

    assert withdrawn.status_code == 503
    assert withdrawn.json()["error"]["code"] == "STATISTICS_WITHDRAWN"
    assert str(run_id) in withdrawn.json()["error"]["detail"]["withdrawn"]["runs"]


def test_approving_more_leaves_the_snapshot_incomplete_not_withdrawn(
    connection, client, make_run, seed_outcomes, reviewer
):
    """The other direction, which must *not* take the page down.

    Every number already published is still true; there is simply more
    now. Served, flagged stale, with the reason stated — because
    refusing here would mean an approval briefly breaking the page.
    """
    make_run(
        status="APPROVED",
        reviewer_id=reviewer,
        period_start=PERIOD_START,
        period_end=PERIOD_START + timedelta(days=30),
    )
    seed_outcomes(40, prefix="FIRST", detected_from=PERIOD_START)
    refresh_public_stats(connection, as_of=AS_OF)

    make_run(
        status="APPROVED",
        reviewer_id=reviewer,
        period_start=PERIOD_START + timedelta(days=60),
        period_end=PERIOD_START + timedelta(days=90),
    )

    response = client.get("/public/stats/win_rate")
    payload = response.json()

    assert response.status_code == 200
    assert payload["freshness"]["stale"] is True
    assert "incomplete but not wrong" in payload["freshness"]["staleness_reason"]


def test_a_refresh_after_withdrawal_restores_service_with_the_smaller_number(
    connection, client, make_run, seed_outcomes, reviewer
):
    """The page comes back, showing less. That is the intended shape of a
    withdrawal: not a hidden correction, a visibly smaller claim."""
    from core.model_validation_evaluation.validation.review import reject

    keep = make_run(
        status="APPROVED",
        reviewer_id=reviewer,
        period_start=PERIOD_START,
        period_end=PERIOD_START + timedelta(days=30),
    )
    drop = make_run(
        status="APPROVED",
        reviewer_id=reviewer,
        period_start=PERIOD_START + timedelta(days=60),
        period_end=PERIOD_START + timedelta(days=90),
    )
    seed_outcomes(20, prefix="KEEP", detected_from=PERIOD_START)
    seed_outcomes(20, prefix="DROP", detected_from=PERIOD_START + timedelta(days=61))
    refresh_public_stats(connection, as_of=AS_OF)
    assert client.get("/public/stats/win_rate").json()["sample_size"] == 40

    reject(connection, drop, user_id=reviewer, note="withdrawn")
    assert client.get("/public/stats/win_rate").status_code == 503

    refresh_public_stats(connection, as_of=AS_OF)
    restored = client.get("/public/stats/win_rate").json()

    assert restored["sample_size"] == 20
    assert restored["provenance"]["approved_runs"] == [str(keep)]


# --------------------------------------------------------------------------
# The live half of the gate
# --------------------------------------------------------------------------


def test_live_outcomes_are_not_published_without_an_approved_window(
    connection, seed_outcomes, lineage
):
    """The decision this module had to make, enforced.

    Outcomes exist, no historical run covers them, and no window has been
    approved. They are invisible — which is the answer to "does a live
    outcome need its own review": yes.
    """
    seed_outcomes(
        40,
        prefix="LIVE",
        detected_from=PERIOD_START.replace(month=7, day=1),
    )

    scope = current_scope(connection)

    assert scope.is_empty
    assert published_dataset(connection, scope, as_of=AS_OF).frame.empty


def test_a_pending_window_publishes_nothing(connection, seed_outcomes, lineage):
    seed_outcomes(40, prefix="PW", detected_from=PERIOD_START.replace(month=7, day=1))
    open_window_for_review(
        connection,
        period_start=LIVE_START,
        period_end=LIVE_END,
        data_snapshot_id=lineage.data_snapshot_id,
    )

    assert current_scope(connection).is_empty


def test_an_approved_window_publishes_its_outcomes(connection, seed_outcomes, lineage, reviewer):
    seed_outcomes(40, prefix="AW", detected_from=PERIOD_START.replace(month=7, day=1))
    open_window_for_review(
        connection,
        period_start=LIVE_START,
        period_end=LIVE_END,
        data_snapshot_id=lineage.data_snapshot_id,
    )
    approve_window(connection, period_start=LIVE_START, period_end=LIVE_END, user_id=reviewer)

    scope = current_scope(connection)
    dataset = published_dataset(connection, scope, as_of=AS_OF)

    assert len(scope.windows) == 1
    assert len(dataset.frame) == 40


def test_a_rejected_window_stops_publishing(connection, seed_outcomes, lineage, reviewer):
    seed_outcomes(40, prefix="RW", detected_from=PERIOD_START.replace(month=7, day=1))
    open_window_for_review(
        connection,
        period_start=LIVE_START,
        period_end=LIVE_END,
        data_snapshot_id=lineage.data_snapshot_id,
    )
    approve_window(connection, period_start=LIVE_START, period_end=LIVE_END, user_id=reviewer)
    assert not current_scope(connection).is_empty

    reject_window(connection, period_start=LIVE_START, period_end=LIVE_END, user_id=reviewer)

    assert current_scope(connection).is_empty


def test_a_window_approval_requires_a_named_human(connection, lineage):
    """An approval nobody's name is on is not a human review, and this
    gate exists precisely so that somebody decided."""
    open_window_for_review(
        connection,
        period_start=LIVE_START,
        period_end=LIVE_END,
        data_snapshot_id=lineage.data_snapshot_id,
    )

    with pytest.raises(PublicStatsError) as exc_info:
        approve_window(connection, period_start=LIVE_START, period_end=LIVE_END, user_id=None)

    assert exc_info.value.code == "REVIEW_REFUSED"


def test_a_window_never_queued_cannot_be_approved(connection, reviewer):
    """Approving a window that was never queued would skip the gate rather
    than pass it."""
    with pytest.raises(PublicStatsError) as exc_info:
        approve_window(connection, period_start=LIVE_START, period_end=LIVE_END, user_id=reviewer)

    assert exc_info.value.code == "RELEASE_NOT_FOUND"


def test_a_successful_live_scan_grants_nothing_on_its_own(connection, seed_outcomes, lineage):
    """Module 19's warning, tested directly.

    `live_scan_runs` is deliberately ungated. A scan completing must not
    become a publication permission through any join — the gate is on
    outcomes, not on scans.
    """
    from core.live_scanner.runs import finish_run as finish_scan
    from core.live_scanner.runs import start_run as start_scan
    from core.live_scanner.schedule import as_of_for
    from infra.db.enums import LiveScanStatus

    scan_date = LIVE_START
    run = start_scan(connection, scan_date=scan_date, as_of=as_of_for(scan_date), lineage=lineage)
    finish_scan(connection, run.id, status=LiveScanStatus.COMPLETED, detail={})
    seed_outcomes(40, prefix="SCAN", detected_from=PERIOD_START.replace(month=7, day=1))

    assert current_scope(connection).is_empty


def test_an_overlapping_run_and_window_do_not_double_count(
    connection, make_run, seed_outcomes, lineage, reviewer
):
    """A replay of recent history overlapping the live window.

    Counted twice, every published statistic inflates — and inflates in
    ARGUS's favour whenever the setup succeeded.
    """
    overlap_start = PERIOD_START.replace(month=7, day=1)
    make_run(
        status="APPROVED",
        reviewer_id=reviewer,
        period_start=overlap_start,
        period_end=overlap_start + timedelta(days=60),
    )
    open_window_for_review(
        connection,
        period_start=LIVE_START,
        period_end=LIVE_END,
        data_snapshot_id=lineage.data_snapshot_id,
    )
    approve_window(connection, period_start=LIVE_START, period_end=LIVE_END, user_id=reviewer)

    seed_outcomes(30, prefix="OVL", detected_from=overlap_start)

    dataset = published_dataset(connection, current_scope(connection), as_of=AS_OF)

    assert len(dataset.frame) == 30
    assert dataset.frame["setup_id"].is_unique


def test_outcomes_labelled_under_a_different_snapshot_are_not_in_an_approved_window(
    connection, seed_outcomes, lineage, reviewer
):
    """A window pins its labelling as well as its dates.

    Module 15's success criterion is an admitted placeholder, so an
    outcome's meaning depends on the snapshot that produced it. A window
    approved under one criterion must not publish outcomes labelled under
    another.
    """
    from core.outcome_tracking.config import OutcomeConfig, publish_outcome_snapshot

    other = publish_outcome_snapshot(
        connection, OutcomeConfig(), as_of=AS_OF, description="a revised criterion"
    )
    seed_outcomes(
        40,
        prefix="OTH",
        detected_from=PERIOD_START.replace(month=7, day=1),
        data_snapshot_id=other,
    )
    open_window_for_review(
        connection,
        period_start=LIVE_START,
        period_end=LIVE_END,
        data_snapshot_id=lineage.data_snapshot_id,
    )
    approve_window(connection, period_start=LIVE_START, period_end=LIVE_END, user_id=reviewer)

    dataset = published_dataset(connection, current_scope(connection), as_of=AS_OF)

    assert dataset.frame.empty
