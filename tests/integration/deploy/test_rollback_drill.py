"""The rollback drill, run against a real schema.

> Rollback reverts the image, never the schema.

Both halves are checked here: that the assessment correctly identifies
which of ARGUS's own revisions would block a code rollback, and that the
schema downgrade — the thing operators reach for during an incident —
refuses by default and genuinely destroys data when it is forced.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from infra.deploy.migrate import pending_migrations, upgrade_to_head
from infra.deploy.rollback import (
    SCHEMA_ROLLBACK_ENV_VAR,
    SchemaRollbackRefused,
    assess_rollback,
    main,
    rollback_schema,
)


@pytest.fixture
def at_head(fresh_engine: Engine, alembic_target) -> Engine:
    upgrade_to_head(fresh_engine)
    return fresh_engine


def test_the_assessment_names_exactly_the_revisions_that_touched_existing_objects(
    at_head: Engine,
):
    """Additive revisions are invisible to old code; the rest are not.

    This used to assert that a rollback to 0012 was outright safe, on
    the grounds that everything after it only added tables. That stopped
    being true at 0020, which drops and rebuilds three unique
    constraints — so the assessment now reports it as blocking, which is
    correct rather than a regression.

    Asserting the *classification* instead keeps what the original test
    was for — the mechanism works, and the head is read from the
    database rather than written down — while staying true as
    migrations accumulate. A new additive migration changes nothing
    here; a new destructive one has to be named, which is the moment to
    think about it.

    The head revision is read from the database rather than written
    down. It used to be a literal, which meant every new migration
    failed this test for no reason connected to what the test is about —
    and a test that has to be edited on every unrelated change is one
    people learn to edit without reading.
    """
    head = pending_migrations(at_head).current
    assessment = assess_rollback("0012", at_head)

    assert assessment.current == head
    # 0020 rebuilds three unique constraints and tightens a column.
    assert {risk.revision for risk in assessment.blocking} == {"0020"}
    # Everything else since 0012 only added tables, columns or triggers.
    additive = {risk.revision for risk in assessment.revisions} - {"0020"}
    assert additive >= {"0013", "0014", "0017", "0018", "0019"}
    assert not assessment.code_rollback_safe


def test_rolling_back_past_a_column_that_was_tightened_is_not_safe(at_head: Engine):
    """0004, 0006, 0007 and 0008 made columns NOT NULL.

    An image from before them would start writing NULLs the schema now
    refuses — which is the exact failure `migrate.py` protects the
    forward direction against.
    """
    assessment = assess_rollback("0003", at_head)
    assert not assessment.code_rollback_safe
    assert {risk.revision for risk in assessment.blocking} >= {"0004", "0006", "0007", "0008"}


def test_rolling_back_to_where_the_database_already_is_has_nothing_to_assess(
    at_head: Engine,
):
    head = pending_migrations(at_head).current
    assessment = assess_rollback(head, at_head)

    assert assessment.revisions == ()
    assert assessment.code_rollback_safe


def test_the_schema_downgrade_refuses_without_an_explicit_override(at_head: Engine):
    """An operator reaching for a downgrade mid-incident should meet a sentence."""
    with pytest.raises(SchemaRollbackRefused) as refused:
        rollback_schema("0012", at_head, env={})

    message = str(refused.value)
    assert "redeploying the previous image" in message
    assert "restore from the pre-deploy backup" in message
    assert SCHEMA_ROLLBACK_ENV_VAR in message


def test_a_forced_downgrade_destroys_data_rather_than_restoring_it(at_head: Engine):
    """The reason the default is a refusal, demonstrated.

    The downgrade "succeeds" and the table 0013 added — the one Module
    24's per-source signup limit counts — is gone, rows included.
    """
    with at_head.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO registration_attempts (ip_address, succeeded) "
                "VALUES ('10.0.0.1', false)"
            )
        )

    rollback_schema("0012", at_head, env={SCHEMA_ROLLBACK_ENV_VAR: "1"})

    assert pending_migrations(at_head).current == "0012"
    with at_head.connect() as connection:
        assert (
            connection.execute(text("SELECT to_regclass('registration_attempts')")).scalar_one()
            is None
        )


def test_the_assessment_command_exits_zero_when_a_rollback_is_safe(
    at_head: Engine, alembic_target, monkeypatch
):
    """Which is what makes it usable in a release script.

    The target is head itself: with nothing applied since, there is
    nothing an old image could trip over. That is a thinner example than
    this test once had — it used 0012, before 0020 made that range
    non-additive — but it is the one that is true, and the exit code is
    what is under test rather than which revisions are additive today.
    """
    _configure(monkeypatch, alembic_target)
    head = pending_migrations(at_head).current

    assert main([str(head)]) == 0


def test_the_assessment_command_exits_one_when_it_is_not(
    at_head: Engine, alembic_target, monkeypatch
):
    _configure(monkeypatch, alembic_target)
    assert main(["0003"]) == 1


def test_the_command_refuses_to_guess_a_target(monkeypatch):
    assert main([]) == 2


def _configure(monkeypatch, url) -> None:
    from packages.config.settings import get_config

    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    monkeypatch.setenv("ARGUS_DATABASE__HOST", url.host or "localhost")
    monkeypatch.setenv("ARGUS_DATABASE__PORT", str(url.port or 5432))
    monkeypatch.setenv("ARGUS_DATABASE__NAME", url.database or "")
    monkeypatch.setenv("ARGUS_DATABASE__USER", url.username or "")
    get_config.cache_clear()
