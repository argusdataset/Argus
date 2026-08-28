"""Retention decisions as data. The measurement itself is an integration test.

Module 24 left "log retention is undefined" and "three tables grow
unbounded" as one line each. They are different problems with different
mechanisms, and the useful property to test here is that every place
ARGUS accumulates something has a *stated* answer rather than a default
nobody chose.
"""

from __future__ import annotations

import pytest

from infra.db.append_only import APPEND_ONLY_TABLES
from infra.deploy.retention import (
    GROWTH_WARNING_BYTES,
    MONITORED_TABLES,
    POLICIES,
    SESSION_RETENTION_DAYS,
)

BY_NAME = {policy.name: policy for policy in POLICIES}


def test_the_three_tables_module_24_flagged_all_have_a_policy():
    for table in ("audit_log", "login_attempts", "registration_attempts"):
        assert table in BY_NAME, f"{table} grows unbounded and has no stated retention"


def test_logs_are_the_platforms_retention_and_ARGUS_says_so():
    """The decision that follows constrains the rest of the system.

    Application logs are diagnostic and disposable; anything that must
    outlive the platform's window is written to `audit_log`. If something
    is only ever logged, it is not retained.
    """
    logs = BY_NAME["application_logs"]
    assert logs.retention_class == "platform"
    assert logs.retain_days is None


def test_sessions_are_the_only_thing_a_delete_job_touches():
    """Everything else that accumulates is either guarded or not in the database."""
    prunable = [policy.name for policy in POLICIES if policy.retention_class == "prunable"]
    assert prunable == ["sessions"]
    assert BY_NAME["sessions"].retain_days == SESSION_RETENTION_DAYS


@pytest.mark.parametrize("table", sorted(MONITORED_TABLES))
def test_a_monitored_table_is_actually_guarded_append_only(table: str):
    """Which is *why* it cannot be pruned, and why it is monitored instead.

    If one of these ever stopped being append-only, the right answer
    would be a delete job rather than a growth projection — so the two
    facts are tied together here rather than left to agree by memory.
    """
    assert table in APPEND_ONLY_TABLES


@pytest.mark.parametrize("table", sorted(MONITORED_TABLES))
def test_a_monitored_table_is_declared_kept_forever(table: str):
    policy = BY_NAME[table]
    assert policy.retention_class == "immutable"
    assert policy.retain_days is None
    assert "trigger" in policy.enforced_by


def test_every_policy_records_who_enforces_it_and_why():
    """A retention table with a blank rationale is a decision nobody made."""
    for policy in POLICIES:
        assert policy.enforced_by.strip()
        assert len(policy.rationale) > 40


def test_the_growth_threshold_is_a_restore_time_control():
    """Set where a `pg_dump` of the table still restores inside the RTO.

    Not a disk-space number — Postgres does not care about a gibibyte.
    See the README on RTO.
    """
    assert GROWTH_WARNING_BYTES == 1024**3


def test_no_policy_promises_a_retention_the_database_would_reject():
    """A finite retention on a guarded table would be a lie in a table.

    The DELETE it implies raises SQLSTATE 23001, so the job could never
    run — and a documented policy nothing enforces is worse than none.
    """
    for policy in POLICIES:
        if policy.retention_class == "immutable":
            assert policy.retain_days is None
