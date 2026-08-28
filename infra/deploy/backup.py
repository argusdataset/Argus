"""Backup and restore, and the verification that makes a backup a backup.

The project's standing principle is that a backup never restored and
tested should not be considered reliable. That makes the interesting
function here `verify_restore`, not `dump` — anyone can call `pg_dump`,
and a dump that has never been read back is a file, not a backup.

## What is actually irreplaceable, and why RPO is not one number

ARGUS's data divides into three classes with genuinely different
recovery stories, and averaging them into a single RPO would overstate
the risk for two of them and understate it for the third:

**Reproducible from the provider.** Every `canonical_*` table. If it were
lost entirely, Module 04/05 could re-ingest it from FMP; the PIT
timestamps would be re-derived and the `availability_time` values would
differ from the originals, which matters for exact reproducibility of a
past backtest but not for the system's ability to operate. Expensive to
lose — a full re-ingest of fifteen years — but not *irrecoverable*.

**Recomputable from code plus data.** Features, states, similarity
results, signals, setups, outcomes. All of it is a deterministic function
of canonical data and a published configuration version, which is the
entire point of Modules 08-17 recording lineage. Losing it costs compute,
not information.

**Irreplaceable.** Three things, and only these:

- **Credentials and accounts** (`users`, `sessions`). An argon2 hash
  cannot be regenerated from anything; losing it means every user
  re-registers.
- **Human judgements** — `historical_scan_status`, `public_release_windows`,
  `setup_outcomes.review_confidence`, `setup_outcomes.false_positive_type`.
  A person looked at a result and made a call. Nothing recomputes that.
- **The audit trail** (`audit_log`, `login_attempts`,
  `registration_attempts`, and the user-owned `user_watchlists`). The
  record of what happened, which by construction cannot be reconstructed
  from what currently exists.

`IRREPLACEABLE_TABLES` names them, and `verify_restore` checks the
restored copy has all of them — because a restore that silently omitted
`users` would look like a success and be a catastrophe.

## Why the append-only guarantee makes verification cheap and necessary

Module 03 enforces immutability with plpgsql triggers, not with
convention. Two consequences for backups:

*Cheap*: a restored copy can be verified by row count alone for the
guarded tables. Rows in those tables never change, so a count that
matches the source is a strong statement, not a weak one — there is no
"same count, different contents" case to worry about.

*Necessary*: `pg_dump` restores triggers only if the dump includes them,
and a data-only dump does not. A restored database whose append-only
triggers are missing looks completely normal and has silently lost the
guarantee the entire project is built on. `verify_restore` counts the
guards and fails if any are absent — which is the check most likely to
catch a real, quiet restore failure.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from infra.db.append_only import APPEND_ONLY_TABLES, NO_DELETE_TABLES
from infra.db.connection import build_database_url
from infra.observability.logging import configure_logging, get_logger

__all__ = [
    "DRILL_SUFFIX",
    "IRREPLACEABLE_TABLES",
    "BackupResult",
    "DrillResult",
    "RestoreVerification",
    "drill",
    "dump",
    "main",
    "restore",
    "verify_restore",
]

_log = get_logger("argus.deploy.backup")

#: The tables whose loss cannot be undone by re-ingesting or recomputing.
#: See the module docstring for the three classes and why only these are
#: in the third.
IRREPLACEABLE_TABLES: tuple[str, ...] = (
    "users",
    "roles",
    "sessions",
    "user_watchlists",
    "user_watchlist_items",
    "audit_log",
    "login_attempts",
    "registration_attempts",
    "historical_scan_status",
    "public_release_windows",
    "setup_outcomes",
)


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    bytes_written: int
    seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "bytes": self.bytes_written,
            "seconds": round(self.seconds, 2),
        }


@dataclass(frozen=True, slots=True)
class RestoreVerification:
    """Whether a restored copy is actually usable. Every field is checked."""

    schema_revision: str | None
    expected_revision: str | None
    tables_present: int
    missing_irreplaceable: tuple[str, ...] = ()
    guard_triggers: int = 0
    expected_guard_triggers: int = 0
    row_counts: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def schema_current(self) -> bool:
        return self.schema_revision is not None and self.schema_revision == self.expected_revision

    @property
    def guards_intact(self) -> bool:
        return self.guard_triggers >= self.expected_guard_triggers

    @property
    def usable(self) -> bool:
        return self.schema_current and not self.missing_irreplaceable and self.guards_intact

    def as_dict(self) -> dict[str, Any]:
        return {
            "usable": self.usable,
            "schema_revision": self.schema_revision,
            "expected_revision": self.expected_revision,
            "schema_current": self.schema_current,
            "tables_present": self.tables_present,
            "missing_irreplaceable": list(self.missing_irreplaceable),
            "guard_triggers": self.guard_triggers,
            "expected_guard_triggers": self.expected_guard_triggers,
            "guards_intact": self.guards_intact,
            "row_counts": self.row_counts,
            "seconds": round(self.seconds, 2),
        }


def dump(database_url: str, destination: Path, *, timeout: int = 900) -> BackupResult:
    """`pg_dump` in custom format, schema and data together.

    Custom format (`-Fc`) rather than plain SQL: it is compressed, it can
    be restored selectively, and `pg_restore` can parallelise it. Schema
    *and* data rather than data-only, because a data-only dump does not
    carry Module 03's append-only triggers and a restore from one would
    quietly drop the immutability guarantee — see the module docstring.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    subprocess.run(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--file",
            str(destination),
            _libpq(database_url),
        ],
        check=True,
        capture_output=True,
        timeout=timeout,
    )

    elapsed = time.monotonic() - started
    result = BackupResult(
        path=destination,
        bytes_written=destination.stat().st_size,
        seconds=elapsed,
    )
    _log.info("backup written", extra={"event": "backup_written", **result.as_dict()})
    return result


def restore(archive: Path, database_url: str, *, timeout: int = 1800) -> float:
    """`pg_restore` into an existing, empty database. Returns seconds taken.

    The target must exist and be empty — `pg_restore` does not create it,
    and restoring over a populated database produces a confusing mix of
    both. Creating the target is deliberately the caller's step, because
    on a managed platform it is usually a console action rather than a
    command.
    """
    started = time.monotonic()

    completed = subprocess.run(
        [
            "pg_restore",
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            "--dbname",
            _libpq(database_url),
            str(archive),
        ],
        capture_output=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"pg_restore failed ({completed.returncode}): "
            f"{completed.stderr.decode('utf-8', 'replace')[-2000:]}"
        )

    elapsed = time.monotonic() - started
    _log.info(
        "restore completed",
        extra={"event": "restore_completed", "seconds": round(elapsed, 2)},
    )
    return elapsed


def verify_restore(
    engine: Engine,
    *,
    expected_revision: str | None = None,
    started_at: float | None = None,
) -> RestoreVerification:
    """Prove a restored database is usable. Four checks, all of them load-bearing.

    1. **The schema is at the expected revision.** A restore that landed
       one migration behind produces errors that look like application
       bugs.
    2. **Every irreplaceable table exists.** A restore missing `users`
       looks like a success from the outside.
    3. **The append-only triggers are present.** The check most likely to
       catch a quiet failure — a database without them behaves normally
       and has lost the project's central guarantee.
    4. **Row counts for the irreplaceable tables.** Reported rather than
       asserted here: what counts as "the right number" belongs to
       whoever ran the drill and knows what the source held.
    """
    from infra.observability.health import expected_revision as head_revision

    expected = expected_revision or head_revision()
    guards = len(APPEND_ONLY_TABLES) + len(NO_DELETE_TABLES)

    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()

        present = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
        }

        # `pg_trigger`, not `information_schema.triggers`. The
        # information_schema view is doubly wrong for this check: it
        # reports one row per *event*, so a `BEFORE UPDATE OR DELETE`
        # guard counts twice, and it omits TRUNCATE triggers entirely
        # because the SQL standard has no TRUNCATE. Between them those
        # two errors nearly cancel — 42 against an expected 44 — which is
        # the worst possible outcome for a check, because the number
        # looks almost right. The first run of this drill produced
        # exactly that and reported `usable: false` on a restore that was
        # in fact perfect.
        #
        # Counting TRUNCATE guards is not incidental here. A table whose
        # row-level guard survived a restore and whose TRUNCATE guard did
        # not is erasable by a single statement while looking completely
        # protected — which is precisely the quiet failure this function
        # exists to catch, and precisely the one the information_schema
        # view cannot see.
        trigger_count = connection.execute(
            text(
                "SELECT count(*) FROM pg_trigger t "
                "JOIN pg_proc p ON p.oid = t.tgfoid "
                "WHERE p.proname = 'argus_reject_mutation' "
                "AND NOT t.tgisinternal"
            )
        ).scalar_one()

        counts: dict[str, int] = {}
        for table in IRREPLACEABLE_TABLES:
            if table in present:
                counts[table] = int(
                    connection.execute(
                        text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
                    ).scalar_one()
                )

    verification = RestoreVerification(
        schema_revision=revision,
        expected_revision=expected,
        tables_present=len(present),
        missing_irreplaceable=tuple(
            table for table in IRREPLACEABLE_TABLES if table not in present
        ),
        # Each guarded table gets two triggers: one row-level, one for
        # TRUNCATE. `infra/db/append_only.py` installs both, and counting
        # the pair is what would notice a restore that carried one and
        # dropped the other.
        guard_triggers=int(trigger_count),
        expected_guard_triggers=guards * 2,
        row_counts=counts,
        seconds=(time.monotonic() - started_at) if started_at else 0.0,
    )

    _log.info(
        "restore verified",
        extra={"event": "restore_verified", **verification.as_dict()},
    )
    return verification


def default_archive_path(root: Path | None = None) -> Path:
    """A timestamped archive name, so two drills never collide."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = root or Path("/tmp/argus-backups")
    return base / f"argus-{stamp}.dump"


def _dsn(url: URL) -> str:
    """A `URL` with its password actually in it, for the two callers that need one.

    `URL.__str__` masks the password — deliberately, so a logged URL does
    not leak a credential, and `build_database_url` says so in its own
    docstring. `pg_dump` and `pg_restore` are subprocesses that genuinely
    need the real DSN, so they get it here, at one call site, rather than
    by every caller remembering which of the two string forms is which.

    The first version of the drill did not do this and passed `***` as
    the password to a subprocess, which surfaced as an authentication
    failure rather than as anything resembling its cause.
    """
    return url.render_as_string(hide_password=False)


def _libpq(url: str) -> str:
    """SQLAlchemy's URL form to the one `pg_dump` understands.

    SQLAlchemy writes `postgresql+psycopg://`; libpq accepts
    `postgresql://` and rejects the driver suffix. One translation, in one
    place, rather than every caller remembering.
    """
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


#: Appended to the source database's name to make the drill's target. A
#: drill that could be pointed at an arbitrary database is one keystroke
#: from being a drill that overwrites production, so the target is
#: derived rather than given, and `drill` refuses if the two names come
#: out equal.
DRILL_SUFFIX = "_restore_drill"


@dataclass(frozen=True, slots=True)
class DrillResult:
    """One end-to-end proof that the backup can be turned back into a system."""

    source: str
    target: str
    backup: BackupResult
    restore_seconds: float
    verification: RestoreVerification

    @property
    def passed(self) -> bool:
        return self.verification.usable

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "passed": self.passed,
            "backup": self.backup.as_dict(),
            "restore_seconds": round(self.restore_seconds, 2),
            "verification": self.verification.as_dict(),
        }


def drill(
    source_url: str,
    *,
    target_database: str | None = None,
    archive: Path | None = None,
) -> DrillResult:
    """Dump, restore into a fresh database, and verify. The whole loop.

    A backup nobody has restored is a file, not a backup — so this is a
    command rather than a procedure in a document, and it runs against a
    real target rather than asserting anything about one.

    The target is **created and dropped by this function**. It is derived
    from the source name with `DRILL_SUFFIX` rather than accepted as a
    URL, because a restore drill that takes a destination is a restore
    drill that can be aimed at production by a typo. Passing
    `target_database` explicitly is still possible for a test, and the
    equality check still refuses the one value that matters.

    Never logs a URL. The database *name* identifies the drill; the URL
    carries a password, and this project's rule is that credentials reach
    `SecretsProvider` and nothing else.
    """
    url = make_url(source_url)
    source_name = url.database or ""
    target_name = target_database or f"{source_name}{DRILL_SUFFIX}"

    if not source_name:
        raise ValueError("Source URL names no database; refusing to guess one.")
    if target_name == source_name:
        raise ValueError(
            f"Restore drill target and source are both {source_name!r}. Refusing: a "
            "drill that restores over its own source destroys the thing it was "
            "meant to prove recoverable."
        )

    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{target_name}"'))
            connection.execute(text(f'CREATE DATABASE "{target_name}"'))
    finally:
        admin.dispose()

    destination = archive or default_archive_path()
    written = dump(_dsn(url), destination)

    target_url = url.set(database=target_name)
    started = time.monotonic()
    restore_seconds = restore(destination, _dsn(target_url))

    target_engine = create_engine(target_url)
    try:
        verification = verify_restore(target_engine, started_at=started)
    finally:
        target_engine.dispose()

    result = DrillResult(
        source=source_name,
        target=target_name,
        backup=written,
        restore_seconds=restore_seconds,
        verification=verification,
    )
    _log.info("restore drill complete", extra={"event": "restore_drill", **result.as_dict()})
    return result


def main(argv: list[str] | None = None) -> int:
    """`python -m infra.deploy.backup` — run the drill against the configured database.

    Exit `0` when the restored copy verified usable, `1` when it did not
    and `2` when the drill could not run at all. A failing drill is a
    failing exit code on purpose: this is the command a scheduled job or
    a release checklist runs, and a drill whose failure has to be noticed
    by reading output is a drill nobody notices failing.
    """
    configure_logging()
    _ = argv
    try:
        result = drill(_dsn(build_database_url()))
    except Exception as error:  # noqa: BLE001 - the caller must see any failure
        _log.exception(
            "restore drill could not run",
            extra={"event": "restore_drill_error", "error_type": type(error).__name__},
        )
        return 2

    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":  # pragma: no cover - an operator's command
    raise SystemExit(main())
