"""Migrations as a pre-deploy step, against a real database.

The requirement is that migrations run before traffic routes to new code.
On Railway that is a `preDeployCommand`: it runs to completion, and a
non-zero exit abandons the deploy so the old containers keep serving.
Two halves have to be true for that to mean anything — the command has to
actually migrate, and it has to actually fail when it should.
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from infra.db.append_only import APPEND_ONLY_TABLES
from infra.deploy import migrate
from infra.deploy.migrate import (
    BackwardsIncompatibleMigration,
    pending_migrations,
    upgrade_to_head,
)
from infra.deploy.processes import PRE_DEPLOY_COMMAND
from packages.config.settings import get_config


def test_an_unmigrated_database_reports_every_revision_as_pending(
    fresh_engine: Engine, alembic_target
):
    plan = pending_migrations(fresh_engine)
    assert plan.current is None
    assert plan.head is not None
    assert len(plan.pending) >= 13
    assert not plan.up_to_date


def test_the_pre_deploy_step_takes_an_empty_database_to_head(fresh_engine: Engine, alembic_target):
    """The first deploy's whole job, run for real."""
    upgrade_to_head(fresh_engine)

    after = pending_migrations(fresh_engine)
    assert after.current == after.head
    assert after.up_to_date


def test_the_schema_it_produces_carries_the_append_only_guards(
    fresh_engine: Engine, alembic_target
):
    """A migration that reached head without the triggers would look fine.

    Counted from `pg_trigger`, not `information_schema.triggers` — the
    latter omits TRUNCATE triggers entirely, which is exactly the half
    that would be missing.
    """
    upgrade_to_head(fresh_engine)

    with fresh_engine.connect() as connection:
        guards = connection.execute(
            text(
                "SELECT count(*) FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
                "WHERE p.proname = 'argus_reject_mutation' AND NOT t.tgisinternal"
            )
        ).scalar_one()

    assert guards >= len(APPEND_ONLY_TABLES) * 2


def test_running_it_twice_is_a_no_op(fresh_engine: Engine, alembic_target):
    """Every web container inherits the result; only one runs the command.

    If a second run were not harmless, the failure would appear only
    under a concurrent deploy.
    """
    upgrade_to_head(fresh_engine)
    again = upgrade_to_head(fresh_engine)
    assert again.up_to_date


def test_the_first_deploy_applies_ARGUS_s_destructive_migrations(
    fresh_engine: Engine, alembic_target
):
    """0004, 0006, 0007 and 0008 tighten columns and drop a constraint.

    They are genuinely backwards-incompatible and genuinely have to
    apply to a fresh database. The exemption is real, not a loophole:
    against a schema that does not exist, no previous code is running.
    """
    plan = pending_migrations(fresh_engine)
    assert set(plan.destructive) >= {"0004", "0006", "0007", "0008"}

    upgrade_to_head(fresh_engine)
    assert pending_migrations(fresh_engine).up_to_date


def test_a_destructive_migration_is_refused_once_the_database_is_migrated(
    fresh_engine: Engine, alembic_target, monkeypatch
):
    """The rolling-deploy window: old containers still serving on the new schema.

    Simulated by migrating first, then presenting a pending destructive
    revision — which is exactly the state a second deploy would be in.
    """
    upgrade_to_head(fresh_engine)
    current = pending_migrations(fresh_engine).current

    monkeypatch.setattr(
        migrate,
        "pending_migrations",
        lambda engine=None: migrate.MigrationPlan(
            current=current, head="0099", pending=("0099",), destructive=("0099",)
        ),
    )

    try:
        migrate.upgrade_to_head(fresh_engine)
    except BackwardsIncompatibleMigration as refused:
        assert "0099" in str(refused)
    else:  # pragma: no cover - the check did not fire
        raise AssertionError("a destructive migration was applied without refusal")


def test_the_entrypoint_returns_zero_on_success(fresh_engine, alembic_target, monkeypatch):
    """Railway reads the exit code and nothing else.

    Run through `main()` rather than `upgrade_to_head()` so the whole
    pre-deploy path is exercised: it resolves its own engine from
    configuration, which is what a container does and what a test that
    passes an engine would skip.
    """
    monkeypatch.setenv("DATABASE_URL", alembic_target.render_as_string(hide_password=False))
    monkeypatch.setenv("ARGUS_DATABASE__HOST", alembic_target.host or "localhost")
    monkeypatch.setenv("ARGUS_DATABASE__PORT", str(alembic_target.port or 5432))
    monkeypatch.setenv("ARGUS_DATABASE__NAME", alembic_target.database or "")
    monkeypatch.setenv("ARGUS_DATABASE__USER", alembic_target.username or "")
    get_config.cache_clear()
    try:
        assert migrate.main([]) == 0
    finally:
        get_config.cache_clear()

    assert pending_migrations(fresh_engine).up_to_date


def test_the_entrypoint_returns_two_when_it_refuses(monkeypatch):
    """Distinct from 1. A refusal is a decision; a crash is not.

    Both abandon the deploy, and an operator reading a deploy log should
    be able to tell which happened without reading the log.
    """

    def _refuse(engine=None):
        raise BackwardsIncompatibleMigration("nope")

    monkeypatch.setattr(migrate, "upgrade_to_head", _refuse)
    assert migrate.main([]) == 2


def test_the_configured_pre_deploy_command_invokes_this_module():
    """Ties the tested code to the string the platform will actually run."""
    assert PRE_DEPLOY_COMMAND.endswith("infra.deploy.migrate")
