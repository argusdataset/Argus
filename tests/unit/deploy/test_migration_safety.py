"""Migration safety, as source analysis. No database.

`assert_backwards_compatible` protects one specific window: during a
rolling deploy the *previous* containers are still serving against an
already-migrated schema. These tests are about the decision it makes, not
about applying anything — the integration suite does that.
"""

from __future__ import annotations

import pytest

from infra.deploy.migrate import (
    DESTRUCTIVE_OPERATIONS,
    BackwardsIncompatibleMigration,
    MigrationPlan,
    assert_backwards_compatible,
    destructive_operations,
)

ADDITIVE = """
def upgrade() -> None:
    op.add_column("users", sa.Column("nickname", sa.Text(), nullable=True))
    op.create_index("ix_users_nickname", "users", ["nickname"])


def downgrade() -> None:
    op.drop_column("users", "nickname")
"""

TIGHTENING = """
def upgrade() -> None:
    op.alter_column("users", "email", nullable=False)


def downgrade() -> None:
    op.alter_column("users", "email", nullable=True)
"""

INDEX_ONLY = """
def upgrade() -> None:
    op.drop_index("ix_users_nickname", table_name="users")


def downgrade() -> None:
    op.create_index("ix_users_nickname", "users", ["nickname"])
"""


def test_an_additive_upgrade_is_not_flagged():
    assert destructive_operations(ADDITIVE, function="upgrade") == ()


def test_tightening_a_column_to_not_null_is_flagged():
    """Old code inserting a NULL into that column starts failing mid-deploy."""
    assert destructive_operations(TIGHTENING, function="upgrade") == ("alter_column",)


def test_the_downgrade_body_is_not_read_when_checking_an_upgrade():
    """Every downgrade in this project is destructive by construction.

    Reading both would flag every revision and make the check useless.
    """
    assert destructive_operations(ADDITIVE, function="upgrade") == ()
    assert destructive_operations(ADDITIVE, function="downgrade") == ("drop_column",)


def test_dropping_an_index_is_not_treated_as_destructive():
    """It changes how a query performs, never whether it succeeds.

    Old code keeps working — slower at worst. Flagging it would make the
    check cry wolf on the one maintenance operation that is safe
    mid-deploy.
    """
    assert "drop_index" not in DESTRUCTIVE_OPERATIONS
    assert destructive_operations(INDEX_ONLY, function="upgrade") == ()


def test_a_plan_with_nothing_destructive_passes():
    assert_backwards_compatible(MigrationPlan(current="0012", head="0013", pending=("0013",)))


def test_a_destructive_plan_is_refused_on_an_already_migrated_database():
    plan = MigrationPlan(current="0012", head="0013", pending=("0013",), destructive=("0013",))
    with pytest.raises(BackwardsIncompatibleMigration, match="0013"):
        assert_backwards_compatible(plan)


def test_the_refusal_says_how_to_proceed():
    """An error during a deploy is read by somebody who has to decide something."""
    plan = MigrationPlan(current="0012", head="0013", pending=("0013",), destructive=("0013",))
    with pytest.raises(BackwardsIncompatibleMigration) as refused:
        assert_backwards_compatible(plan)
    message = str(refused.value)
    assert "two deploys" in message
    assert "ARGUS_ALLOW_DESTRUCTIVE_MIGRATION" in message


def test_a_first_deploy_is_exempt():
    """`current is None` means no previous code is running against this schema.

    The property being protected is not in play, so the check's premise
    is false rather than merely inconvenient. This is what lets ARGUS's
    existing 0004/0006/0007/0008 reach a fresh database in one step.
    """
    assert_backwards_compatible(
        MigrationPlan(current=None, head="0013", pending=("0001",), destructive=("0004",))
    )


def test_the_override_is_explicit_and_per_deploy():
    plan = MigrationPlan(current="0012", head="0013", pending=("0013",), destructive=("0013",))
    assert_backwards_compatible(plan, env={"ARGUS_ALLOW_DESTRUCTIVE_MIGRATION": "1"})


@pytest.mark.parametrize("value", ["true", "yes", "on", "1 ", ""])
def test_a_truthy_looking_override_that_is_not_exactly_one_does_not_count(value: str):
    """`true`, `yes` and `on` are not the value. One spelling, deliberately.

    A flag that accepts several spellings is a flag somebody sets by
    accident, and this one lets a migration through that would break
    running code.
    """
    plan = MigrationPlan(current="0012", head="0013", pending=("0013",), destructive=("0013",))
    with pytest.raises(BackwardsIncompatibleMigration):
        assert_backwards_compatible(plan, env={"ARGUS_ALLOW_DESTRUCTIVE_MIGRATION": value})


def test_an_absent_override_is_not_an_error():
    """`env={}` is a deployment with the flag unset, not a broken call."""
    plan = MigrationPlan(current="0012", head="0013", pending=("0013",), destructive=("0013",))
    with pytest.raises(BackwardsIncompatibleMigration):
        assert_backwards_compatible(plan, env={})


def test_the_override_does_not_reach_the_process_environment(monkeypatch):
    """Injectable rather than read by name, like every sibling in this module.

    It also keeps this out of Module 24's `scan_secret_provider_bypass`,
    which flags a named `os.environ` read outside `SecretsProvider` — it
    cannot tell a deploy flag from a credential, and should not have to.
    """
    monkeypatch.delenv("ARGUS_ALLOW_DESTRUCTIVE_MIGRATION", raising=False)
    plan = MigrationPlan(current="0012", head="0013", pending=("0013",), destructive=("0013",))
    assert_backwards_compatible(plan, env={"ARGUS_ALLOW_DESTRUCTIVE_MIGRATION": "1"})


def test_a_plan_knows_whether_there_is_anything_to_do():
    assert MigrationPlan(current="0013", head="0013", pending=()).up_to_date
    assert not MigrationPlan(current="0012", head="0013", pending=("0013",)).up_to_date


# --------------------------------------------------------------------------
# The rule, run over this repository's own migrations
#
# Everything above tests the mechanism against migrations the test itself
# writes. That is the right way to test a rule — and it meant the rule had
# never been applied to the artefacts it protects. Migration 0020 rebuilds
# three unique constraints, which is genuinely backwards-incompatible, and
# nobody found out until the deploy refused it and six web services failed
# their health checks against a schema one revision behind.
#
# So these run `destructive_operations` over the real `versions/`
# directory. A destructive migration is not forbidden — it is required to
# be *declared*, with what an operator has to do about it. The failure
# then happens at commit time, in a message that says what to write, and
# not in a production deploy log.
# --------------------------------------------------------------------------


def _repository_migrations() -> dict[str, str]:
    """Every migration in `versions/`, by revision id, as source text."""
    from infra.deploy.migrate import ALEMBIC_INI

    versions = ALEMBIC_INI.parent / "migrations" / "versions"
    sources: dict[str, str] = {}
    for path in sorted(versions.glob("[0-9]*.py")):
        sources[path.name.split("_", 1)[0]] = path.read_text()
    return sources


def test_the_scan_finds_this_repositorys_migrations():
    """A guard over an empty set passes forever.

    The assertions below are subtractions, so an empty scan satisfies
    them trivially — a renamed directory or a changed filename convention
    would turn the whole section into a tautology without failing.
    """
    found = _repository_migrations()

    assert len(found) >= 20
    assert "0001" in found and "0020" in found


def test_every_destructive_migration_is_acknowledged():
    """The test that would have caught 0020 before it reached a deploy.

    Failing here means a migration drops or alters something and nobody
    said so. That is not a reason to change the migration — rebuilding a
    unique constraint requires dropping it, and there is no other way —
    it is a reason to write down what the deploy needs, in
    `ACKNOWLEDGED_DESTRUCTIVE`, so the refusal is expected rather than
    discovered from a failed health check.
    """
    from infra.deploy.migrate import ACKNOWLEDGED_DESTRUCTIVE, destructive_operations

    undeclared = {
        revision: destructive_operations(source, function="upgrade")
        for revision, source in _repository_migrations().items()
        if destructive_operations(source, function="upgrade")
        and revision not in ACKNOWLEDGED_DESTRUCTIVE
    }

    assert undeclared == {}, (
        f"these migrations would be refused by a deploy and are not declared: "
        f"{undeclared}. Add each to ACKNOWLEDGED_DESTRUCTIVE with what an operator "
        "has to do — either split it across two deploys, or set "
        "ARGUS_ALLOW_DESTRUCTIVE_MIGRATION=1 for one deploy and why that is safe."
    )


def test_no_acknowledgement_outlives_the_migration_it_describes():
    """A stale entry is worse than none.

    It tells the next reader that a revision needs care when it does not,
    and it makes the list something people stop trusting — at which point
    the test above stops being read too.
    """
    from infra.deploy.migrate import ACKNOWLEDGED_DESTRUCTIVE, destructive_operations

    sources = _repository_migrations()
    for revision in ACKNOWLEDGED_DESTRUCTIVE:
        assert revision in sources, f"{revision} is acknowledged but no longer exists"
        assert destructive_operations(sources[revision], function="upgrade"), (
            f"{revision} is acknowledged as destructive but its upgrade() no longer "
            "does anything destructive — remove the entry"
        )


def test_an_acknowledgement_says_what_to_do_about_it():
    """A reason, not a name.

    The value is what an operator reads at 2am when a deploy refuses, so
    it has to name the remedy rather than restate the problem.
    """
    from infra.deploy.migrate import _ESCAPE_ENV_VAR, ACKNOWLEDGED_DESTRUCTIVE

    for revision, reason in ACKNOWLEDGED_DESTRUCTIVE.items():
        assert len(reason.split()) >= 20, f"{revision}'s acknowledgement is too thin"
        assert _ESCAPE_ENV_VAR in reason or "two deploys" in reason, (
            f"{revision}'s acknowledgement does not say how to proceed"
        )
