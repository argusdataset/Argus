"""Classifying failures — the decision that makes the whole policy different.

Module 17's batch replay has one response to failure. This module has
three, and they are only worth having if the classification is right, so
these tests are mostly about the boundaries: what looks transient and is
not, what looks fatal and is not.
"""

from __future__ import annotations

import pytest
from sqlalchemy import exc as sa_exc

from core.live_scanner.failures import FailureClass, classify_failure, describe_failure
from core.model_validation_evaluation.validation.versions import (
    ConsistencyReport,
    ReplayIntent,
    VersionMismatch,
)


def _dbapi(kind: type[sa_exc.DBAPIError]) -> sa_exc.DBAPIError:
    return kind("SELECT 1", {}, Exception("boom"))


def test_a_dropped_connection_is_transient():
    assert classify_failure(_dbapi(sa_exc.OperationalError)) is FailureClass.TRANSIENT
    assert classify_failure(_dbapi(sa_exc.InterfaceError)) is FailureClass.TRANSIENT


def test_a_pool_timeout_is_transient():
    assert classify_failure(sa_exc.TimeoutError("pool exhausted")) is FailureClass.TRANSIENT


def test_a_constraint_violation_is_never_transient():
    """The trap a naive rule falls into.

    `IntegrityError` is a `DBAPIError`, so "any database error is
    transient" would retry a constraint violation forever — and it will
    be violated identically on every attempt.
    """
    assert classify_failure(_dbapi(sa_exc.IntegrityError)) is FailureClass.PERMANENT


def test_a_programming_error_is_permanent():
    """A bad query does not get better on a retry, and does not belong to
    a security."""
    assert classify_failure(_dbapi(sa_exc.ProgrammingError)) is FailureClass.PERMANENT


def test_a_version_mismatch_is_permanent_and_not_worth_retrying():
    """Correction 3's refusal. The fix is to publish a version or check
    out different code, and both are a person's job."""
    from datetime import UTC, datetime

    report = ConsistencyReport(
        intent=ReplayIntent.REPLAY,
        period_start=datetime(2026, 3, 3, tzinfo=UTC),
        period_end=datetime(2026, 3, 3, tzinfo=UTC),
        findings=[],
    )

    assert classify_failure(VersionMismatch(report)) is FailureClass.PERMANENT


def test_an_ordinary_computation_error_is_worth_asking_about_per_security():
    """Not assumed to be one security's fault — assumed to be *possibly*
    one security's fault, which is a question the scanner then asks."""
    assert classify_failure(ValueError("nan in close series")) is FailureClass.ISOLATABLE
    assert classify_failure(KeyError("missing column")) is FailureClass.ISOLATABLE
    assert classify_failure(ZeroDivisionError()) is FailureClass.ISOLATABLE


def test_classification_defaults_against_retrying():
    """A misclassified transient costs a missed day the next run picks up.
    A misclassified permanent hides a real problem behind a retry loop.
    The second is worse, so nothing is transient unless it is recognised.
    """

    class Exotic(Exception):
        pass

    assert classify_failure(Exotic("who knows")) is not FailureClass.TRANSIENT


def test_a_dbapi_error_flagged_as_an_invalidated_connection_is_transient():
    """How SQLAlchemy reports a mid-statement disconnect."""
    error = _dbapi(sa_exc.DataError)
    error.connection_invalidated = True

    assert classify_failure(error) is FailureClass.TRANSIENT


def test_the_stored_description_says_what_was_decided_and_why():
    """A stored failure has to explain not just what broke but why the
    scanner responded the way it did."""
    described = describe_failure(ValueError("bad bar"))

    assert described["type"] == "ValueError"
    assert "bad bar" in described["message"]
    assert described["classification"] == FailureClass.ISOLATABLE.value


def test_a_very_long_message_is_truncated_rather_than_stored_whole():
    """A JSONB column is not a place to put a megabyte of pandas repr."""
    described = describe_failure(ValueError("x" * 10_000))

    assert len(described["message"]) <= 2000


@pytest.mark.parametrize(
    "error",
    [
        sa_exc.DisconnectionError("server closed the connection"),
        ConnectionError("network unreachable"),
        TimeoutError("timed out"),
    ],
)
def test_network_level_failures_are_transient(error: BaseException):
    assert classify_failure(error) is FailureClass.TRANSIENT
