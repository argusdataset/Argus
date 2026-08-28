"""Rollback: what can actually be undone, and what only looks like it can.

A deploy that goes wrong has to be reversible, and "reversible" gets
casually applied to two different things that behave nothing alike.

**Code rolls back.** Railway keeps previous deployments and can
redeploy one; the containers running the bad build are replaced by
containers running the previous image. That is a supported, one-click
operation and it is the rollback ARGUS actually relies on.

**Schema does not.** `alembic downgrade` runs a `downgrade()` that, in
this project, drops tables and columns. Running it does not return the
database to a previous state — it destroys whatever the new schema was
holding. Calling that a rollback is how a bad afternoon becomes a
restore from backup.

## Why rolling back code alone is enough

Because `infra/deploy/migrate.py` refuses a migration that is not
backwards-compatible with the code already running. That check exists for
the rolling-deploy window, but it buys this as well: if the new schema is
safe for the *old* code during a deploy, it is equally safe for the old
code after a rollback. The schema stays forward; the code steps back;
the two still work together.

That is the whole design, and it is worth stating as a rule rather than
leaving it implied:

> **Rollback reverts the image, never the schema.**

`assess_rollback` checks that the rule holds for a specific target rather
than asserting it. It walks the revisions between the target and head and
reports the ones whose `upgrade()` was destructive — because those are
exactly the ones that were allowed through by the first-deploy exemption
or by an explicit override, and so are exactly the ones where old code
might not survive after all.

## What to do when the rule does not hold

Nothing clever. `rollback_schema` exists and refuses by default, and the
error says the honest thing: if a destructive migration has to come back
out, the recovery path is a restore from the pre-deploy backup, not a
downgrade. `infra/deploy/backup.py` is the other half of this module, and
the reason the runbook takes a backup immediately before a migration that
had to be forced.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from infra.deploy.migrate import (
    ALEMBIC_INI,
    destructive_operations,
    pending_migrations,
)
from infra.observability.logging import configure_logging, get_logger

__all__ = [
    "SCHEMA_ROLLBACK_ENV_VAR",
    "RevisionRisk",
    "RollbackAssessment",
    "SchemaRollbackRefused",
    "assess_rollback",
    "main",
    "rollback_schema",
]

_log = get_logger("argus.deploy.rollback")

#: Set to `1` for one command to permit a schema downgrade. Deliberately
#: not a flag on the function: an operator who has to put this in the
#: environment has to type the words "allow schema rollback", which is a
#: better prompt to stop and think than a `--force` nobody reads.
SCHEMA_ROLLBACK_ENV_VAR = "ARGUS_ALLOW_SCHEMA_ROLLBACK"


class SchemaRollbackRefused(RuntimeError):
    """A schema downgrade was requested without the explicit override."""


@dataclass(frozen=True, slots=True)
class RevisionRisk:
    """One revision between the rollback target and head, and what it did."""

    revision: str
    upgrade_operations: tuple[str, ...] = ()
    downgrade_operations: tuple[str, ...] = ()

    @property
    def blocks_code_rollback(self) -> bool:
        """Whether reverting the image alone leaves old code on a schema it cannot use.

        Keyed on the *upgrade*. A revision whose upgrade only added
        things is invisible to old code; one that tightened a column or
        dropped a constraint is not.
        """
        return bool(self.upgrade_operations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "upgrade_operations": list(self.upgrade_operations),
            "downgrade_operations": list(self.downgrade_operations),
            "blocks_code_rollback": self.blocks_code_rollback,
        }


@dataclass(frozen=True, slots=True)
class RollbackAssessment:
    """Whether redeploying an earlier image is safe against the current schema."""

    current: str | None
    target: str
    revisions: tuple[RevisionRisk, ...] = field(default_factory=tuple)

    @property
    def blocking(self) -> tuple[RevisionRisk, ...]:
        return tuple(item for item in self.revisions if item.blocks_code_rollback)

    @property
    def code_rollback_safe(self) -> bool:
        """True when the old image can run against the schema as it stands."""
        return not self.blocking

    @property
    def schema_rollback_lossy(self) -> bool:
        """True when undoing the schema would destroy data rather than restore it."""
        return any(item.downgrade_operations for item in self.revisions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "target": self.target,
            "revisions": [item.as_dict() for item in self.revisions],
            "code_rollback_safe": self.code_rollback_safe,
            "schema_rollback_lossy": self.schema_rollback_lossy,
            "blocking": [item.revision for item in self.blocking],
        }


def assess_rollback(target: str, engine: Any | None = None) -> RollbackAssessment:
    """What redeploying the image that shipped `target` would run into.

    Read-only, and safe to run while the bad deploy is still serving —
    which is when it is actually needed. `target` is the Alembic revision
    the old image expects, which is the revision that was head when it
    was built.
    """
    scripts = ScriptDirectory.from_config(_config())
    plan = pending_migrations(engine)
    current = plan.current

    risks: list[RevisionRisk] = []
    for revision in _between(target, current, scripts):
        source = Path(scripts.get_revision(revision).path).read_text()
        risks.append(
            RevisionRisk(
                revision=revision,
                upgrade_operations=destructive_operations(source, function="upgrade"),
                downgrade_operations=destructive_operations(source, function="downgrade"),
            )
        )

    assessment = RollbackAssessment(current=current, target=target, revisions=tuple(risks))
    _log.info(
        "rollback assessed",
        extra={"event": "rollback_assessed", **assessment.as_dict()},
    )
    return assessment


def rollback_schema(
    target: str,
    engine: Any | None = None,
    *,
    env: dict[str, str] | None = None,
) -> RollbackAssessment:
    """Downgrade the schema to `target`. Refuses unless explicitly overridden.

    Kept in the codebase rather than left to a shell command precisely so
    that it can refuse: an operator reaching for a downgrade during an
    incident should meet a sentence explaining that a restore is the
    recovery path, not a command that silently does the wrong thing.
    """
    source = env if env is not None else dict(os.environ)
    assessment = assess_rollback(target, engine)

    if source.get(SCHEMA_ROLLBACK_ENV_VAR) != "1":
        raise SchemaRollbackRefused(
            f"Refusing to downgrade the schema to {target}. Rolling back ARGUS means "
            "redeploying the previous image; the schema stays where it is, which is "
            "safe because migrate.py refuses a migration the previous code cannot run "
            "against. "
            + (
                "A downgrade from here would drop data rather than restore it — the "
                "recovery path for that is a restore from the pre-deploy backup "
                "(infra/deploy/backup.py). "
                if assessment.schema_rollback_lossy
                else ""
            )
            + f"Set {SCHEMA_ROLLBACK_ENV_VAR}=1 for this command if you have decided "
            "otherwise and have a backup."
        )

    _log.warning(
        "downgrading schema by explicit override",
        extra={"event": "schema_rollback", **assessment.as_dict()},
    )
    command.downgrade(_config(), target)
    return assessment


def main(argv: list[str] | None = None) -> int:
    """`python -m infra.deploy.rollback <revision>` — assess, never act.

    Assessment only, deliberately. The command that actually rolls back
    is Railway's, and a CLI here that could perform a downgrade is a
    footgun sitting in the same container as the incident.
    """
    configure_logging()
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        _log.error(
            "no target revision given",
            extra={"event": "rollback_usage", "usage": "python -m infra.deploy.rollback <rev>"},
        )
        return 2

    assessment = assess_rollback(args[0])
    return 0 if assessment.code_rollback_safe else 1


def _config() -> Config:
    return Config(str(ALEMBIC_INI))


def _between(target: str, current: str | None, scripts: ScriptDirectory) -> list[str]:
    """Revisions applied after `target`, up to and including `current`.

    Empty when the database is already at or below the target, which is
    the "nothing to undo" case and not an error.
    """
    if current is None or current == target:
        return []
    walked = [item.revision for item in scripts.iterate_revisions(current, target)]
    return [revision for revision in walked if revision != target][::-1]


if __name__ == "__main__":  # pragma: no cover - an operator's command
    raise SystemExit(main())
