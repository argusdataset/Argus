"""The review gate: nothing reaches Module 20 without a person's name on it.

The gate is the only thing standing between an unvalidated model's numbers
and a page that presents them as a track record. So the tests here are
weighted towards what it *refuses* — an approval nobody signed, an
approval of a run that was never queued, a run whose approval was later
revoked and must stop being served.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Connection

from core.model_validation_evaluation.validation.review import (
    ReviewRefused,
    approve,
    approved_runs,
    current_status,
    is_approved,
    open_for_review,
    reject,
    review_history,
)
from core.model_validation_evaluation.validation.runs import finish_run, start_run
from core.scoring.engine import Lineage
from infra.db.enums import HistoricalScanStatus, ValidationRunStatus
from infra.db.schema.validation import historical_scan_status
from tests.integration.model_validation_evaluation.conftest import (
    PERIOD_END,
    PERIOD_START,
)


@pytest.fixture
def reviewer(connection: Connection):
    """A real `users` row — `assigned_by_user_id` is a foreign key."""
    role_id = connection.execute(
        text("INSERT INTO roles (name) VALUES (:name) RETURNING id"),
        {"name": f"module17-reviewer-{uuid4()}"},
    ).scalar_one()
    return connection.execute(
        text(
            "INSERT INTO users (email, display_name, role_id) "
            "VALUES (:email, 'Module 17 Reviewer', :role) RETURNING id"
        ),
        {"email": f"reviewer-{uuid4()}@example.test", "role": role_id},
    ).scalar_one()


@pytest.fixture
def completed_run(connection: Connection, lineage: Lineage):
    run = start_run(
        connection,
        lineage=lineage,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        notes="module17 review-gate test",
    )
    finish_run(connection, run.id, status=ValidationRunStatus.COMPLETED)
    return run


def test_a_completed_run_starts_pending_review_set_by_the_system(
    connection: Connection, completed_run
):
    """The one row that legitimately has nobody's name on it."""
    record = open_for_review(connection, completed_run.id)

    assert record.status is HistoricalScanStatus.PENDING_REVIEW
    assert record.by_system
    assert record.assigned_by_user_id is None


def test_a_pending_run_is_not_approved_and_module_20_would_not_see_it(
    connection: Connection, completed_run
):
    open_for_review(connection, completed_run.id)

    assert not is_approved(connection, completed_run.id)
    assert completed_run.id not in approved_runs(connection)


def test_approval_requires_a_named_human(connection: Connection, completed_run):
    """An approval nobody signed is not a human review, and the module
    will not record one as if it were."""
    open_for_review(connection, completed_run.id)

    with pytest.raises(ReviewRefused) as exc_info:
        approve(connection, completed_run.id, user_id=None)
    assert "requires a reviewer" in str(exc_info.value)


def test_an_approved_run_becomes_queryable_as_approved(
    connection: Connection, completed_run, reviewer
):
    open_for_review(connection, completed_run.id)
    record = approve(connection, completed_run.id, user_id=reviewer, note="looks sound")

    assert record.status is HistoricalScanStatus.APPROVED
    assert record.assigned_by_user_id == reviewer
    assert is_approved(connection, completed_run.id)
    assert completed_run.id in approved_runs(connection)


def test_a_run_approved_then_rejected_stops_being_served(
    connection: Connection, completed_run, reviewer
):
    """Current status, not "has an APPROVED row anywhere".

    A plain `WHERE status = 'APPROVED'` would keep serving this run
    forever, which is the specific mistake `approved_runs` exists to make
    impossible rather than merely discouraged.
    """
    open_for_review(connection, completed_run.id)
    approve(connection, completed_run.id, user_id=reviewer)
    reject(connection, completed_run.id, user_id=reviewer, note="sample too thin")

    assert not is_approved(connection, completed_run.id)
    assert completed_run.id not in approved_runs(connection)
    # The APPROVED row is still there — the history is the point.
    rows = (
        connection.execute(
            select(historical_scan_status.c.status).where(
                historical_scan_status.c.model_validation_run_id == completed_run.id
            )
        )
        .scalars()
        .all()
    )
    assert "APPROVED" in rows


def test_a_rejected_run_can_be_approved_later_and_both_decisions_survive(
    connection: Connection, completed_run, reviewer
):
    """Rejection is not deletion. The run, its signals and its report all
    stay; what changes is whether Module 20 may serve them."""
    open_for_review(connection, completed_run.id)
    reject(connection, completed_run.id, user_id=reviewer, note="wait for more data")
    approve(connection, completed_run.id, user_id=reviewer, note="second look")

    history = review_history(connection, completed_run.id)

    assert [record.status.value for record in history] == [
        "PENDING_REVIEW",
        "REJECTED",
        "APPROVED",
    ]
    assert is_approved(connection, completed_run.id)


def test_a_reviewers_judgement_cannot_be_overwritten_by_the_next_one(
    connection: Connection, completed_run, reviewer
):
    """Append-only, enforced by Module 03's trigger rather than by
    convention."""
    open_for_review(connection, completed_run.id)
    approve(connection, completed_run.id, user_id=reviewer, note="original judgement")

    with pytest.raises(Exception) as exc_info:
        connection.execute(
            text(
                "UPDATE historical_scan_status SET note = 'rewritten' "
                "WHERE model_validation_run_id = :run"
            ),
            {"run": completed_run.id},
        )
    assert "append-only" in str(exc_info.value)


def test_a_run_that_was_never_queued_cannot_be_approved(
    connection: Connection, completed_run, reviewer
):
    """Approving a run that never entered the queue would skip the gate
    rather than pass it."""
    with pytest.raises(ReviewRefused) as exc_info:
        approve(connection, completed_run.id, user_id=reviewer)

    assert "never been opened for review" in str(exc_info.value)


def test_a_running_or_failed_run_cannot_be_offered_for_approval(
    connection: Connection, lineage: Lineage
):
    """Partial numbers must not be presented to a reviewer as results."""
    running = start_run(
        connection, lineage=lineage, period_start=PERIOD_START, period_end=PERIOD_END
    )

    with pytest.raises(ReviewRefused) as exc_info:
        open_for_review(connection, running.id)
    assert "not COMPLETED" in str(exc_info.value)

    finish_run(connection, running.id, status=ValidationRunStatus.FAILED)
    with pytest.raises(ReviewRefused):
        open_for_review(connection, running.id)


def test_opening_an_already_pending_run_does_not_add_a_second_row(
    connection: Connection, completed_run
):
    """Re-running the completion step is not a second review event."""
    open_for_review(connection, completed_run.id)
    open_for_review(connection, completed_run.id)

    assert len(review_history(connection, completed_run.id)) == 1


def test_a_run_that_does_not_exist_is_refused_rather_than_silently_queued(connection: Connection):
    with pytest.raises(ReviewRefused) as exc_info:
        open_for_review(connection, uuid4())
    assert "No validation run" in str(exc_info.value)


def test_approved_runs_returns_only_the_approved_ones_among_several(
    connection: Connection, lineage: Lineage, reviewer
):
    """The query has to partition per run, not filter globally."""
    runs = []
    for index in range(3):
        run = start_run(
            connection,
            lineage=lineage,
            period_start=PERIOD_START + timedelta(days=index),
            period_end=PERIOD_END,
        )
        finish_run(connection, run.id, status=ValidationRunStatus.COMPLETED)
        open_for_review(connection, run.id)
        runs.append(run)

    approve(connection, runs[0].id, user_id=reviewer)
    reject(connection, runs[1].id, user_id=reviewer)
    # runs[2] stays PENDING_REVIEW.

    served = approved_runs(connection)

    assert runs[0].id in served
    assert runs[1].id not in served
    assert runs[2].id not in served


def test_current_status_is_none_for_a_run_that_was_never_queued(
    connection: Connection, completed_run
):
    """None and PENDING_REVIEW are different facts: never offered for
    review versus offered and not yet decided."""
    assert current_status(connection, completed_run.id) is None


def test_three_decisions_in_one_transaction_still_order_correctly(
    connection: Connection, completed_run, reviewer
):
    """The bug migration 0008 exists for.

    `assigned_at` defaults to `now()`, which in PostgreSQL is transaction
    *start* time — so all three of these rows share a timestamp to the
    microsecond — and the `id` tiebreak is a random UUID. Ordering on those
    made "the current status" a coin flip. It passed in production only
    because reviews normally arrive in separate transactions, which is the
    dangerous kind of passing.
    """
    open_for_review(connection, completed_run.id)
    approve(connection, completed_run.id, user_id=reviewer)
    reject(connection, completed_run.id, user_id=reviewer)

    stamps = {record.assigned_at for record in review_history(connection, completed_run.id)}
    assert len(stamps) == 1, "the premise: one transaction, one timestamp"

    assert [record.sequence_number for record in review_history(connection, completed_run.id)] == [
        0,
        1,
        2,
    ]
    assert current_status(connection, completed_run.id).status is HistoricalScanStatus.REJECTED
    assert not is_approved(connection, completed_run.id)


def test_two_rows_cannot_claim_the_same_sequence_for_one_run(connection: Connection, completed_run):
    """The uniqueness constraint is what makes the read-then-write safe.

    Two concurrent reviewers reading the same maximum both try to write
    the same number; one is refused by the database rather than silently
    producing two rows that claim to be the same assignment.
    """
    from sqlalchemy.exc import IntegrityError

    open_for_review(connection, completed_run.id)

    with pytest.raises(IntegrityError) as exc_info:
        connection.execute(
            historical_scan_status.insert().values(
                model_validation_run_id=completed_run.id,
                sequence_number=0,
                status=HistoricalScanStatus.APPROVED.value,
            )
        )
    assert "uq_scan_status_run_sequence" in str(exc_info.value)
