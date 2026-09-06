"""Migrations as a deployment step that runs before traffic, and can refuse.

The requirement is "run before traffic routes to new code, not after",
which on Railway is a pre-deploy command: it runs to completion against
the new image, and the deploy is abandoned if it exits non-zero — the old
containers keep serving. That ordering is the entire safety property, and
it is worth being precise about what it does and does not buy.

## What running migrations first protects against, and what it does not

Running first means new code never sees an old schema. It does **not**
mean old code never sees a new one: during a rolling deploy the previous
containers are still serving against the already-migrated database. So a
migration must be **backwards-compatible with the code currently
running** — additive columns, new tables, new indexes — and a change that
is not (dropping a column, renaming one, tightening a constraint) has to
be split across two deploys.

`assert_backwards_compatible` checks this rather than trusting it, and
checking found that the assumption does not hold: **four of ARGUS's
thirteen migrations are not backwards-compatible**. 0004, 0006 and 0008
tighten a column to `NOT NULL`; 0007 does that and drops a unique
constraint. Any of them applied while previous code was still inserting
a NULL into that column would fail that code's writes.

That has never mattered, because ARGUS has never been deployed — there
has never been previous code serving. It matters from the first deploy
onward, and it is the reason this check exists rather than a reason to
weaken it.

The **first** deploy is the exception, and it is a real one rather than a
convenience: against a database with no `alembic_version` row at all,
nothing is running against that schema, so there is no previous code to
break and the check's premise is simply false. `pending_migrations`
reports `current=None` for exactly that case and
`assert_backwards_compatible` passes it through.

## Why this wraps Alembic rather than being a shell command

`alembic upgrade head` in a `preDeployCommand` would work and would be
one line. This exists instead because three things are worth doing around
it that a shell string cannot:

1. **Log structurally.** A deploy that fails on a migration should be
   diagnosable from the platform's log viewer, in the same JSON shape as
   everything else — not as an Alembic traceback in a different format.
2. **Configure logging first**, which claims ownership and stops
   `fileConfig` replacing the application's handler. Module 23's whole
   incident was about that call, and running Alembic from inside a
   process that has already configured logging is the exact scenario its
   regression test covers.
3. **Report what it is about to do**, and refuse a destructive migration
   before applying it rather than after.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from infra.db.connection import create_db_engine
from infra.deploy.cli import refuse_arguments
from infra.observability.logging import configure_logging, get_logger

__all__ = [
    "ACKNOWLEDGED_DESTRUCTIVE",
    "ALEMBIC_INI",
    "DESTRUCTIVE_OPERATIONS",
    "BackwardsIncompatibleMigration",
    "MigrationPlan",
    "assert_backwards_compatible",
    "destructive_operations",
    "main",
    "pending_migrations",
    "upgrade_to_head",
]

_log = get_logger("argus.deploy.migrate")

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "db" / "alembic.ini"

#: Alembic operations that break a container still running the previous
#: code. Each is legitimate — none is forbidden outright — but each has
#: to be split across two deploys rather than applied while old code is
#: still serving.
#:
#: Deliberately conservative: `drop_constraint` is here even though
#: dropping a *unique* constraint loosens rather than tightens and would
#: not break old code. Telling a loosening `drop_constraint` from a
#: tightening one means knowing the constraint's type, which is not
#: reliably readable from the call site — and a check that occasionally
#: asks for a second look is a better failure than one that misses the
#: case it exists for.
DESTRUCTIVE_OPERATIONS: tuple[str, ...] = (
    "drop_column",
    "drop_table",
    "drop_constraint",
    "alter_column",
    "rename_table",
)

#: `op.drop_index` is deliberately absent. Dropping an index changes how
#: a query performs and never whether it succeeds, so old code keeps
#: working — slower at worst. Treating it as destructive would make the
#: check cry wolf on the one maintenance operation that is genuinely safe
#: mid-deploy.

#: Migrations whose `upgrade()` is knowingly backwards-incompatible, and
#: what the operator has to do about each. A revision listed here is
#: **still refused** by `assert_backwards_compatible` — this is not an
#: allow-list. It exists so that the refusal is *expected* rather than
#: discovered from a failed deploy, and so a reader can find out why
#: without reconstructing the reasoning.
#:
#: `tests/unit/deploy/test_migration_safety.py` runs the real rule over
#: the repository's own migrations and fails when a destructive one is
#: not listed. Before that test existed, the check only ever ran against
#: synthetic migrations written by its own test — so it was correct,
#: well tested, and had never been pointed at the artefacts it protects.
#: Migration 0020 reached production and failed the deploy there, which
#: is the expensive way to learn it.
ACKNOWLEDGED_DESTRUCTIVE: dict[str, str] = {
    # The four that tighten columns to NOT NULL, and one that rebuilds a
    # constraint. All were applied to a database that did not exist yet,
    # where `assert_backwards_compatible` exempts them by design: with no
    # previous code running against a schema, the property being
    # protected is not in play. They are listed rather than special-cased
    # because a restore onto a database already at an earlier revision
    # would meet the same refusal, and the answer then is the same one.
    "0004": (
        "Tightens columns to NOT NULL. Applied on the first deploy, where the "
        "no-previous-code exemption covers it. Reapplying it to an existing "
        "database would need the two-deploy split or "
        "ARGUS_ALLOW_DESTRUCTIVE_MIGRATION=1."
    ),
    "0006": (
        "Tightens columns to NOT NULL. Same first-deploy exemption and the same "
        "remedy as 0004 if it is ever met on an existing database — split across "
        "two deploys or ARGUS_ALLOW_DESTRUCTIVE_MIGRATION=1."
    ),
    "0007": (
        "Rebuilds a constraint and tightens a column. Same first-deploy exemption "
        "as 0004; on an existing database it needs the two-deploy split or "
        "ARGUS_ALLOW_DESTRUCTIVE_MIGRATION=1 for one deploy."
    ),
    "0008": (
        "Replaces an `assigned_at` ordering with a monotonic counter and tightens "
        "it to NOT NULL — the fix for issue A1's original bug. First-deploy "
        "exemption; otherwise two deploys or ARGUS_ALLOW_DESTRUCTIVE_MIGRATION=1."
    ),
    "0020": (
        "Rebuilds three unique constraints — dropping and recreating is the only "
        "way to change a constraint's columns or its NULL handling — and adds "
        "institutional_ownership.content_fingerprint as NOT NULL. Code from before "
        "this revision names the old constraint in its ON CONFLICT clause and does "
        "not supply the new column, so it genuinely breaks: the refusal is correct. "
        "Ship it as the second of two deploys, or set "
        "ARGUS_ALLOW_DESTRUCTIVE_MIGRATION=1 for one deploy once the only writer of "
        "those three tables (the ingestion cron, replaced wholesale by the same "
        "deploy) is running the new code."
    ),
}

_ESCAPE_ENV_VAR = "ARGUS_ALLOW_DESTRUCTIVE_MIGRATION"

#: Session-scoped Postgres advisory lock key that serializes every
#: process's preDeploy migration step against every other's.
#:
#: Every process now runs this module as its own preDeploy command (see
#: `infra/deploy/processes.py`), because a single owner left the other
#: eight services racing ahead of a refused migration in production —
#: they started new code against a schema `identity` alone was still
#: trying to advance, and failed their health checks. Making the step
#: universal only helps if the N containers that now call
#: `upgrade_to_head` within the same few seconds do not also race *each
#: other*: Alembic's version table is not a lock, so two concurrent
#: `command.upgrade(..., "head")` calls contend for the same DDL at the
#: Postgres lock manager, and the loser does not find the work already
#: done — it fails with an "already exists" style error once the winner
#: commits.
#:
#: The value is arbitrary; it only has to be stable across every process
#: that imports this module and distinct from any other advisory lock
#: ARGUS ever takes (there are none today). Advisory locks key on the
#: connected database as well as this number, so isolated per-test
#: databases (see `tests/integration/deploy/`) never contend with each
#: other or with a real deploy.
_MIGRATION_LOCK_KEY = 279_402_006_009  # no meaning beyond being unique


class BackwardsIncompatibleMigration(RuntimeError):
    """A pending migration would break the code that is still serving."""


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """What a deploy is about to apply."""

    current: str | None
    head: str | None
    pending: tuple[str, ...]
    destructive: tuple[str, ...] = ()

    @property
    def up_to_date(self) -> bool:
        return not self.pending

    def as_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "head": self.head,
            "pending": list(self.pending),
            "destructive": list(self.destructive),
            "up_to_date": self.up_to_date,
        }


def _config() -> Config:
    return Config(str(ALEMBIC_INI))


def pending_migrations(engine: Any | None = None) -> MigrationPlan:
    """Which revisions a deploy would apply, and whether any is destructive.

    Read-only. Safe to call from anywhere, including a health check, and
    called by `upgrade_to_head` before it applies anything.
    """
    scripts = ScriptDirectory.from_config(_config())
    head = scripts.get_current_head()

    resolved = engine if engine is not None else create_db_engine()
    with resolved.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()

    pending = [
        revision.revision
        for revision in scripts.walk_revisions(base="base", head="heads")
        if _is_pending(revision.revision, current, scripts)
    ]
    pending.reverse()

    return MigrationPlan(
        current=current,
        head=head,
        pending=tuple(pending),
        destructive=tuple(_destructive(pending, scripts)),
    )


def assert_backwards_compatible(plan: MigrationPlan, *, env: dict[str, str] | None = None) -> None:
    """Refuse a migration that would break the code still serving.

    Passes unconditionally on a first deploy (`current is None`): there
    is no previous code running against a schema that does not exist yet,
    so the property being protected is not in play. This is what lets
    ARGUS's existing 0004/0006/0007/0008 — which do tighten columns to
    NOT NULL — reach a fresh production database in one step.

    `env` is injectable, like every other environment read in this
    module's siblings (`config.profile_for`,
    `scanner.ScannerSettings.from_environment`,
    `rollback.rollback_schema`): a test can describe a deployment without
    mutating the process it runs in. It also keeps this out of Module
    24's `scan_secret_provider_bypass`, which flags any named
    `os.environ` read outside `SecretsProvider` — correctly, since it
    cannot tell a deploy flag from a credential, and a check that has to
    make that judgement is a check that will one day make it wrongly.

    Otherwise overridable by `ARGUS_ALLOW_DESTRUCTIVE_MIGRATION=1`,
    because the operation is legitimate once the code that depended on
    the old shape is gone — the second of the two deploys a breaking
    change is split into. An escape hatch that has to be set
    deliberately, per deploy, is the difference between a check and an
    obstacle.
    """
    if not plan.destructive:
        return
    if plan.current is None:
        _log.info(
            "destructive operations permitted on an unmigrated database",
            extra={
                "event": "first_deploy_migration",
                "revisions": list(plan.destructive),
            },
        )
        return
    source = env if env is not None else dict(os.environ)
    if source.get(_ESCAPE_ENV_VAR) == "1":
        _log.warning(
            "applying a destructive migration by explicit override",
            extra={
                "event": "destructive_migration_allowed",
                "revisions": list(plan.destructive),
            },
        )
        return

    raise BackwardsIncompatibleMigration(
        f"Revision(s) {', '.join(plan.destructive)} contain operations that would "
        "break containers still running the previous code, which keep serving "
        "throughout a rolling deploy. Split the change across two deploys — ship the "
        "code that stops using the old shape first, then this migration — or set "
        f"{_ESCAPE_ENV_VAR}=1 for this deploy if that has already happened."
    )


@contextlib.contextmanager
def _migration_lock(engine: Any) -> Iterator[None]:
    """Hold `_MIGRATION_LOCK_KEY` for the duration of the check-and-apply below.

    A dedicated connection, so the lock's lifetime is independent of
    whatever connection `pending_migrations` or `command.upgrade` open for
    themselves. Released by explicitly unlocking (so the next waiter does
    not sit through this process's own cleanup) and then closing the
    connection regardless (so the lock is released even if this process
    is killed between acquiring it and the explicit unlock — Postgres
    drops every session-level advisory lock a session held the moment
    that session ends, with no manual cleanup required).
    """
    connection = engine.connect()
    try:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
        yield
    finally:
        with contextlib.suppress(Exception):
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY})
        connection.close()


def upgrade_to_head(
    engine: Any | None = None, *, env: dict[str, str] | None = None
) -> MigrationPlan:
    """Apply every pending migration. Returns the plan that was applied.

    Guarded by `_migration_lock`: every process's preDeploy step calls
    this now, so the check-then-apply sequence has to be atomic across
    processes, not just within one. Whichever process acquires the lock
    first does the real work (or hits the refusal below and raises);
    every process that was waiting then re-reads the plan and finds
    either the schema already at head — the fast path just below — or
    the identical pending, destructive plan the winner also saw, and
    raises the identical refusal. Either the whole fleet advances
    together or the whole fleet's deploy is refused together; there is no
    state in between where some processes see one schema and others see
    another.
    """
    resolved = engine if engine is not None else create_db_engine()
    with _migration_lock(resolved):
        plan = pending_migrations(resolved)

        if plan.up_to_date:
            _log.info(
                "schema already at head",
                extra={"event": "migrations_up_to_date", "revision": plan.current},
            )
            return plan

        assert_backwards_compatible(plan, env=env)

        _log.info(
            "applying migrations",
            extra={
                "event": "migrations_applying",
                "from_revision": plan.current,
                "to_revision": plan.head,
                "count": len(plan.pending),
            },
        )
        command.upgrade(_config(), "head")
        _log.info(
            "migrations applied",
            extra={"event": "migrations_applied", "revision": plan.head},
        )
        return plan


def main(argv: list[str] | None = None) -> int:
    """The pre-deploy entrypoint. Non-zero abandons the deploy."""
    refuse_arguments("infra.deploy.migrate", argv)
    configure_logging()
    try:
        upgrade_to_head()
    except BackwardsIncompatibleMigration as refused:
        _log.error(
            "migration refused",
            extra={"event": "migration_refused", "detail": str(refused)},
        )
        return 2
    except Exception as error:  # noqa: BLE001 - the deploy must see any failure
        _log.exception(
            "migration failed",
            extra={"event": "migration_failed", "error_type": type(error).__name__},
        )
        return 1
    return 0


def _is_pending(revision: str, current: str | None, scripts: ScriptDirectory) -> bool:
    """Whether `revision` is at or after the database's current position."""
    if current is None:
        return True
    applied = {item.revision for item in scripts.iterate_revisions(current, "base")}
    return revision not in applied


def destructive_operations(source: str, *, function: str = "upgrade") -> tuple[str, ...]:
    """Which backwards-incompatible operations one migration function calls.

    Read from the migration file's source rather than by executing it —
    the whole point is to decide *before* applying. Reads one function at
    a time because `upgrade` and `downgrade` are asked different
    questions: a destructive `upgrade` is a deploy that has to be split
    in two, while a destructive `downgrade` is the ordinary case
    (`infra/deploy/rollback.py` uses it to say what a rollback would
    cost).
    """
    body = _function_body(source, function)
    return tuple(operation for operation in DESTRUCTIVE_OPERATIONS if f"op.{operation}(" in body)


def _destructive(revisions: list[str], scripts: ScriptDirectory) -> list[str]:
    """Revisions whose `upgrade()` would break code that is still serving."""
    flagged: list[str] = []
    for revision in revisions:
        script = scripts.get_revision(revision)
        source = Path(script.path).read_text()
        if destructive_operations(source, function="upgrade"):
            flagged.append(revision)
    return flagged


def _function_body(source: str, function: str) -> str:
    """Just the named function, so the other one never triggers the check."""
    pattern = rf"\ndef {function}\(\)[^\n]*:\n(.*?)(?=\ndef |\Z)"
    match = re.search(pattern, source, re.S)
    return match.group(1) if match else ""


if __name__ == "__main__":  # pragma: no cover - the pre-deploy entrypoint
    raise SystemExit(main(sys.argv[1:]))
