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
