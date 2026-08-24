"""One trading day's scan, run unattended, with a policy for every way it can go wrong.

This module calls Module 17's `scan_one_date` and reimplements none of
what happens inside it. What it adds is everything a batch replay does not
need because a human is watching it: a readiness gate, retry with backoff,
per-security quarantine, a durable record of every attempt, and a refusal
to re-do work that is already done.

## The connection is a factory, and that is not incidental

`run_scan` takes something that *opens* a connection rather than an open
one. A retry policy needs this: when a transaction dies from a dropped
connection, the connection is poisoned and the transaction is aborted, so
retrying the same call on the same handle fails identically forever. A
retry that cannot get a fresh transaction is not a retry.

It also lets one attempt span **several** transactions, which it must.
The run row is opened and committed *before* the scan's own transaction
starts, so a process killed mid-scan leaves a row in RUNNING naming the
date that was in flight. Had the row been written inside the scan's
transaction, the crash would roll it back and a hard failure would be
indistinguishable from a scan that never started — which for an
unattended process is the difference between noticing and not.

The scan's own work stays in a single transaction, so a failure discards
its partial writes rather than leaving half a day in the tables.

## The order of the policy, and why

1. **Already done?** Stop. Idempotency starts here rather than relying on
   downstream `ON CONFLICT` clauses to absorb the duplicate work — those
   protect correctness, this protects the promise that a scan runs once.
2. **Not a trading day?** Stop, and write nothing.
3. **Versions consistent?** Correction 3, on every scan. A live scan under
   drifted code attributes today's signals to a version that did not
   produce them, which is the same corruption Module 17 refuses for a
   replay and is *more* likely here, because a deploy happens between
   scans and nobody re-reads the lineage afterwards.
4. **Data ready?** If not, record DATA_NOT_READY and stop. Not a failure.
5. **Scan.** On a transient failure, back off and try again. On anything
   else, probe for a bad security, quarantine it, and try the day again
   without it.

## What this returns

A `ScanOutcome`: the run record and its counts. Not the signals. Module
17's `ReplayResult` holds them because its evaluation reads them
immediately; nothing downstream of a live scan does. A consumer reads
`signals` and `setups` — see `results.py` — and a scanner that handed a
day's signals around in memory would be sized by the universe rather than
by the work.
"""

from __future__ import annotations

import time as time_module
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Connection

from core.data_validation.calendar import is_trading_day
from core.data_validation.universe import list_universe_members_as_of
from core.live_scanner.config import ScannerConfig
from core.live_scanner.failures import FailureClass, classify_failure, describe_failure
from core.live_scanner.readiness import check_readiness
from core.live_scanner.runs import ScanRun, finish_run, latest_run, record_not_ready, start_run
from core.live_scanner.schedule import as_of_for
from core.model_validation_evaluation.validation.config import ValidationConfig
from core.model_validation_evaluation.validation.replay import (
    ModuleConfigs,
    ReplayRequest,
    ScanDateResult,
    compute_features_in_chunks,
    scan_one_date,
)
from core.model_validation_evaluation.validation.versions import (
    ReplayIntent,
    require_consistent_versions,
)
from core.scoring.engine import Lineage
from infra.db.enums import LiveScanStatus

__all__ = [
    "ConnectionFactory",
    "Excluded",
    "ScanOutcome",
    "isolate_failing_securities",
    "run_scan",
]

#: Something that opens a fresh connection-and-transaction. In production
#: this is `engine.begin`; in tests it is a savepoint factory. Either way
#: each attempt gets its own, which is what makes a retry a retry.
ConnectionFactory = Callable[[], AbstractContextManager[Connection]]

#: Injected so tests do not actually wait. Production passes nothing.
Sleeper = Callable[[float], None]


class _AttemptFailed(Exception):
    """One attempt's failure, carrying the run row it left open.

    Internal. The retry loop needs two things from a failed attempt: what
    went wrong, so it can classify it, and which `live_scan_runs` row is
    sitting in RUNNING, so it can close that row rather than orphaning it.
    A bare re-raise would carry only the first.
    """

    def __init__(self, error: BaseException, run_id: UUID | None) -> None:
        super().__init__(str(error))
        self.error = error
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class Excluded:
    """One security quarantined so the rest of the day could be scanned."""

    security_id: UUID
    reason: str
    error_type: str

    def as_dict(self) -> dict[str, str]:
        return {
            "security_id": str(self.security_id),
            "reason": self.reason,
            "error_type": self.error_type,
        }


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    """What happened to one scan date. Deliberately small.

    Carries counts and a run record, never the signals themselves — see
    the module docstring. A caller that wants the day's results queries
    the stored rows.
    """

    scan_date: date
    status: LiveScanStatus
    run: ScanRun | None = None
    result: ScanDateResult | None = None
    excluded: list[Excluded] = field(default_factory=list)
    attempts: int = 0
    reason: str = ""
    #: True when the scanner deliberately did nothing — a non-trading day,
    #: or a date already scanned. Distinct from a scan that ran and found
    #: nothing, which is a normal, successful, empty day.
    skipped: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status in (
            LiveScanStatus.COMPLETED,
            LiveScanStatus.COMPLETED_WITH_EXCLUSIONS,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "scan_date": self.scan_date.isoformat(),
            "status": self.status.value if self.status else None,
            "attempts": self.attempts,
            "skipped": self.skipped,
            "reason": self.reason,
            "excluded": [entry.as_dict() for entry in self.excluded],
            "result": self.result.as_dict() if self.result else None,
            "run_id": str(self.run.id) if self.run else None,
        }


def run_scan(
    connect: ConnectionFactory,
    *,
    scan_date: date,
    lineage: Lineage,
    config: ScannerConfig | None = None,
    validation_config: ValidationConfig | None = None,
    modules: ModuleConfigs | None = None,
    benchmark_security_id: UUID | None = None,
    force: bool = False,
    sleep: Sleeper | None = None,
    now: datetime | None = None,
) -> ScanOutcome:
    """Scan one trading date, unattended, and record what happened.

    `force` re-runs a date that already completed. It exists for a
    deliberate operator action and nothing calls it automatically —
    catch-up and the daily schedule both rely on the completed check, and
    a `force` that anything reached for by default would quietly turn the
    idempotency guarantee into a convention.
    """
    config = config or ScannerConfig()
    modules = modules or ModuleConfigs()
    sleep = sleep or time_module.sleep
    as_of = as_of_for(scan_date, config)

    if not is_trading_day(scan_date):
        return ScanOutcome(
            scan_date=scan_date,
            status=LiveScanStatus.COMPLETED,
            skipped=True,
            reason=(
                f"{scan_date.isoformat()} is not a US equity trading day. Not a scan "
                "that was skipped — a date that was never a scan date, so no run row "
                "is written."
            ),
        )

    with connect() as connection:
        if not force:
            existing = latest_run(connection, scan_date)
            if existing is not None and existing.succeeded:
                return ScanOutcome(
                    scan_date=scan_date,
                    status=existing.status,
                    run=existing,
                    skipped=True,
                    attempts=existing.attempt + 1,
                    reason=(
                        f"Already scanned on attempt {existing.attempt}. Re-running would "
                        "repeat work the storage layer would then mostly discard; the "
                        "check is here so 'runs once' is a property of the scanner "
                        "rather than a side effect of unique indexes."
                    ),
                )

    excluded: list[Excluded] = []
    last_error: BaseException | None = None
    last_run_id: UUID | None = None
    attempts_made = 0

    for attempt in range(config.settings.attempts):
        attempts_made = attempt + 1
        try:
            return _attempt_scan(
                connect,
                scan_date=scan_date,
                as_of=as_of,
                lineage=lineage,
                config=config,
                validation_config=validation_config,
                modules=modules,
                benchmark_security_id=benchmark_security_id,
                excluded=excluded,
                attempts_so_far=attempt + 1,
                now=now,
            )
        except _AttemptFailed as wrapper:
            error = wrapper.error
            last_error = error
            last_run_id = wrapper.run_id
            failure = classify_failure(error)

            retryable = failure is FailureClass.TRANSIENT
            more_attempts = attempt + 1 < config.settings.attempts

            if failure is FailureClass.ISOLATABLE:
                newly = _quarantine(
                    connect,
                    scan_date=scan_date,
                    as_of=as_of,
                    lineage=lineage,
                    modules=modules,
                    benchmark_security_id=benchmark_security_id,
                    already_excluded={entry.security_id for entry in excluded},
                    config=config,
                )
                if newly:
                    over, note = _exclusion_budget(
                        connect,
                        as_of=as_of,
                        lineage=lineage,
                        excluded=excluded + newly,
                        config=config,
                    )
                    if over:
                        return _fail(
                            connect,
                            scan_date=scan_date,
                            as_of=as_of,
                            lineage=lineage,
                            error=error,
                            excluded=excluded + newly,
                            attempts=attempt + 1,
                            reason=note,
                            run_id=last_run_id,
                        )
                    excluded.extend(newly)
                    retryable = True

            if not (retryable and more_attempts):
                break

            # This attempt's row is closed FAILED before the next one
            # opens, so a date that took four tries leaves four rows and
            # the story of the evening is recoverable. `completed_dates`
            # counts only successes, so these intermediate rows cannot
            # make catch-up think the date is done.
            _close_failed_attempt(
                connect,
                run_id=last_run_id,
                scan_date=scan_date,
                as_of=as_of,
                lineage=lineage,
                error=error,
                excluded=excluded,
                attempts=attempt + 1,
                reason=(
                    f"{type(error).__name__} on attempt {attempt}; retrying. {_clip(error, config)}"
                ),
            )
            last_run_id = None
            if failure is FailureClass.TRANSIENT:
                sleep(config.settings.backoff_for(attempt))

    assert last_error is not None
    return _fail(
        connect,
        scan_date=scan_date,
        as_of=as_of,
        lineage=lineage,
        error=last_error,
        excluded=excluded,
        attempts=attempts_made,
        reason=_failure_reason(last_error, excluded, config),
        run_id=last_run_id,
    )


# --------------------------------------------------------------------------
# One attempt
# --------------------------------------------------------------------------


def _attempt_scan(
    connect: ConnectionFactory,
    *,
    scan_date: date,
    as_of: datetime,
    lineage: Lineage,
    config: ScannerConfig,
    validation_config: ValidationConfig | None,
    modules: ModuleConfigs,
    benchmark_security_id: UUID | None,
    excluded: list[Excluded],
    attempts_so_far: int,
    now: datetime | None,
) -> ScanOutcome:
    """One attempt, across three transactions. Raises `_AttemptFailed`.

    The split is deliberate and is explained in the module docstring: the
    gate checks, then a committed RUNNING row, then the scan's own
    transaction. A failure in the third discards the scan's partial writes
    and leaves the row for the caller to close.
    """
    # -- Transaction one: the gates. Nothing is written unless a date is
    # -- genuinely not scannable, in which case that fact is the record.
    with connect() as connection:
        try:
            # Correction 3, on every scan. A deploy happens between scans
            # and nobody re-reads the lineage afterwards, so a live scan
            # is where a silent version drift is *most* likely to go
            # unnoticed.
            require_consistent_versions(
                connection,
                lineage=lineage,
                period_start=as_of,
                period_end=as_of,
                configs=modules.for_version_check(),
                intent=ReplayIntent.REPLAY,
            )
            readiness = check_readiness(
                connection,
                scan_date=scan_date,
                as_of=as_of,
                universe_version_id=lineage.universe_version_id,
                config=config,
            )
        except Exception as error:  # noqa: BLE001 - classified by the caller
            raise _AttemptFailed(error, None) from error

        if not readiness.ready:
            run = record_not_ready(
                connection,
                scan_date=scan_date,
                as_of=as_of,
                lineage=lineage,
                detail={"readiness": readiness.as_dict(), "attempts": attempts_so_far},
                note=readiness.reason,
            )
            return ScanOutcome(
                scan_date=scan_date,
                status=LiveScanStatus.DATA_NOT_READY,
                run=run,
                attempts=attempts_so_far,
                reason=readiness.reason,
            )

    # -- Transaction two: the RUNNING row, committed on its own so a
    # -- process killed mid-scan still leaves it behind.
    with connect() as connection:
        run = start_run(connection, scan_date=scan_date, as_of=as_of, lineage=lineage)

    # -- Transaction three: the scan. A failure here rolls back the
    # -- partial day rather than leaving half of it in the tables.
    try:
        with connect() as connection:
            security_ids = _scannable_universe(
                connection, lineage=lineage, as_of=as_of, excluded=excluded
            )
            result, _signals = scan_one_date(
                connection,
                as_of=as_of,
                request=ReplayRequest(
                    period_start=as_of,
                    period_end=as_of,
                    lineage=lineage,
                    security_ids=security_ids,
                    benchmark_security_id=benchmark_security_id,
                    notes=f"live scan {scan_date.isoformat()}",
                ),
                config=validation_config or ValidationConfig(),
                modules=modules,
            )
            # `_signals` is deliberately dropped. Module 17 returns it
            # because its evaluation reads it immediately; nothing
            # downstream of a live scan does, and holding a
            # universe-sized list in order to throw it away is how a
            # daily process acquires a memory profile it does not need.
    except Exception as error:  # noqa: BLE001 - classified by the caller
        raise _AttemptFailed(error, run.id) from error

    status = LiveScanStatus.COMPLETED_WITH_EXCLUSIONS if excluded else LiveScanStatus.COMPLETED
    with connect() as connection:
        finished = finish_run(
            connection,
            run.id,
            status=status,
            detail={
                "readiness": readiness.as_dict(),
                "result": result.as_dict(),
                "attempts": attempts_so_far,
            },
            excluded=[entry.as_dict() for entry in excluded],
            note=_success_note(result, excluded, config),
            finished_at=now or datetime.now(UTC),
        )

    return ScanOutcome(
        scan_date=scan_date,
        status=status,
        run=finished,
        result=result,
        excluded=list(excluded),
        attempts=attempts_so_far,
        reason=finished.note or "",
    )


# --------------------------------------------------------------------------
# Per-security isolation
# --------------------------------------------------------------------------


def isolate_failing_securities(
    connection: Connection,
    security_ids: list[UUID],
    *,
    as_of: datetime,
    lineage: Lineage,
    modules: ModuleConfigs,
    benchmark_security_id: UUID | None = None,
    config: ScannerConfig | None = None,
) -> list[Excluded]:
    """Which securities throw when computed alone.

    Runs Module 08's own batch path one security at a time —
    `compute_features_in_chunks` with a chunk of one, which Module 17
    proved is result-identical to any other chunk size. So this is the
    same code the scan runs, not a cheaper approximation of it: anything
    the batch would hit in the loader or the feature computation, this
    hits too.

    Two things it deliberately does not catch, both stated rather than
    hidden. A failure arising only from **cross-sectional** code —
    Module 09's ranking over the whole pool — belongs to no single
    security and will find nothing here, which is correct and leaves the
    scan to fail as a whole. And a security whose data is merely *poor*
    rather than malformed is not excluded: Module 08 already reports thin
    history as a `MissReason` and it is not this module's place to
    second-guess that.

    O(N) queries, on the failure path only. That cost is the reason it is
    a fallback rather than a pre-flight check: paying ten thousand
    round trips every evening to guard against a rare bad row would be a
    worse trade than paying them on the rare evening there is one.
    """
    message_limit = (config or ScannerConfig()).settings.message_limit
    failures: list[Excluded] = []
    for security_id in security_ids:
        try:
            compute_features_in_chunks(
                connection,
                [security_id],
                as_of=as_of,
                spec=modules.features,
                feature_schema_version_id=lineage.feature_schema_version_id,
                chunk_size=1,
                market_security_id=benchmark_security_id,
            )
        except Exception as error:  # noqa: BLE001 - the point is to survive any of them
            failures.append(
                Excluded(
                    security_id=security_id,
                    reason=str(error)[:message_limit],
                    error_type=type(error).__name__,
                )
            )
    return failures


def _quarantine(
    connect: ConnectionFactory,
    *,
    scan_date: date,
    as_of: datetime,
    lineage: Lineage,
    modules: ModuleConfigs,
    benchmark_security_id: UUID | None,
    already_excluded: set[UUID],
    config: ScannerConfig,
) -> list[Excluded]:
    """Probe for bad securities in a fresh transaction.

    Fresh, because the transaction that just failed is aborted — every
    statement on it would raise `InFailedSqlTransaction` and the probe
    would report the entire universe as broken.
    """
    with connect() as connection:
        candidates = [
            security_id
            for security_id in _universe(connection, lineage=lineage, as_of=as_of)
            if security_id not in already_excluded
        ]
        return isolate_failing_securities(
            connection,
            candidates,
            as_of=as_of,
            lineage=lineage,
            modules=modules,
            benchmark_security_id=benchmark_security_id,
            config=config,
        )


def _exclusion_budget(
    connect: ConnectionFactory,
    *,
    as_of: datetime,
    lineage: Lineage,
    excluded: list[Excluded],
    config: ScannerConfig,
) -> tuple[bool, str]:
    """Whether too much of the universe has been quarantined to call it a scan.

    Isolating one bad security to save the day is the point. Isolating a
    third of the universe and reporting COMPLETED_WITH_EXCLUSIONS is how a
    broken feed gets filed as a normal Tuesday — and worse, Module 09's
    ranking is cross-sectional, so the surviving names would produce a
    candidate pool that is not comparable to any other day's.
    """
    with connect() as connection:
        universe_size = len(_universe(connection, lineage=lineage, as_of=as_of))

    if universe_size == 0:
        return True, "The universe is empty; there is nothing to exclude from."

    fraction = len(excluded) / universe_size
    limit = config.settings.max_excluded_fraction.value
    # The allowance comes first: one bad security is never a broken feed,
    # and without it the fraction would make a single bad name
    # un-isolatable in any universe smaller than 1/limit — exactly the
    # case per-security isolation exists for.
    if len(excluded) <= config.settings.exclusion_allowance or fraction <= limit:
        return False, ""
    return True, (
        f"{len(excluded)} of {universe_size} securities ({fraction:.1%}) would have to be "
        f"excluded to complete this scan; the limit is {limit:.0%}. That is a broken feed "
        "rather than a few bad names, and Module 09's cross-sectional ranking would make "
        "the surviving pool incomparable to other days'."
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _universe(connection: Connection, *, lineage: Lineage, as_of: datetime) -> list[UUID]:
    return [
        member.security_id
        for member in list_universe_members_as_of(connection, lineage.universe_version_id, as_of)
    ]


def _scannable_universe(
    connection: Connection, *, lineage: Lineage, as_of: datetime, excluded: list[Excluded]
) -> list[UUID] | None:
    """The universe minus anything quarantined, or None on the happy path.

    None means "let Module 17 resolve the universe itself", which is the
    normal case and keeps the scan's universe resolution in exactly one
    place. Only once something has been excluded does this module need to
    name the list.
    """
    if not excluded:
        return None
    quarantined = {entry.security_id for entry in excluded}
    return [
        security_id
        for security_id in _universe(connection, lineage=lineage, as_of=as_of)
        if security_id not in quarantined
    ]


def _fail(
    connect: ConnectionFactory,
    *,
    scan_date: date,
    as_of: datetime,
    lineage: Lineage,
    error: BaseException,
    excluded: list[Excluded],
    attempts: int,
    reason: str,
    run_id: UUID | None = None,
) -> ScanOutcome:
    """Record a FAILED run and return it. Never raises.

    Not re-raising is the whole difference from batch. An unattended
    process that raises crashes, and a crashed scanner is one nobody hears
    from — the failure has to end up in a row something can read, not in a
    stack trace going to a log nobody is watching.

    `run_id` closes the row this attempt already opened. It is None when
    the attempt failed before the row existed (a version mismatch, a dead
    database), and then a row is created so the failure is still on the
    record.
    """
    finished = _write_failure(
        connect,
        run_id=run_id,
        scan_date=scan_date,
        as_of=as_of,
        lineage=lineage,
        error=error,
        excluded=excluded,
        attempts=attempts,
        reason=reason,
    )
    return ScanOutcome(
        scan_date=scan_date,
        status=LiveScanStatus.FAILED,
        run=finished,
        excluded=list(excluded),
        attempts=attempts,
        reason=reason,
    )


def _close_failed_attempt(
    connect: ConnectionFactory,
    *,
    run_id: UUID | None,
    scan_date: date,
    as_of: datetime,
    lineage: Lineage,
    error: BaseException,
    excluded: list[Excluded],
    attempts: int,
    reason: str,
) -> None:
    """Close an attempt that failed and will be retried.

    The row is FAILED rather than left RUNNING, because RUNNING is
    reserved for "in flight or the process died holding it" — the one
    state that means somebody should look. An attempt the scanner itself
    already decided to retry is not that.
    """
    _write_failure(
        connect,
        run_id=run_id,
        scan_date=scan_date,
        as_of=as_of,
        lineage=lineage,
        error=error,
        excluded=excluded,
        attempts=attempts,
        reason=reason,
    )


def _write_failure(
    connect: ConnectionFactory,
    *,
    run_id: UUID | None,
    scan_date: date,
    as_of: datetime,
    lineage: Lineage,
    error: BaseException,
    excluded: list[Excluded],
    attempts: int,
    reason: str,
) -> ScanRun:
    with connect() as connection:
        target = run_id
        if target is None:
            target = start_run(connection, scan_date=scan_date, as_of=as_of, lineage=lineage).id
        return finish_run(
            connection,
            target,
            status=LiveScanStatus.FAILED,
            detail={
                "failure": describe_failure(error),
                "attempts": attempts,
                "excluded_count": len(excluded),
            },
            excluded=[entry.as_dict() for entry in excluded],
            note=reason,
        )


def _clip(error: BaseException, config: ScannerConfig) -> str:
    return str(error)[: config.settings.message_limit]


def _failure_reason(error: BaseException, excluded: list[Excluded], config: ScannerConfig) -> str:
    failure = classify_failure(error)
    if failure is FailureClass.TRANSIENT:
        return (
            f"Retries exhausted against a transient failure ({type(error).__name__}): "
            f"{_clip(error, config)}. The next scheduled run will try this date again."
        )
    if failure is FailureClass.ISOLATABLE and not excluded:
        return (
            f"{type(error).__name__} could not be attributed to any single security — "
            "computing each one alone reproduced nothing. That points at cross-sectional "
            f"code or the scan as a whole rather than at bad data: {_clip(error, config)}"
        )
    return f"{type(error).__name__}: {_clip(error, config)}"


def _success_note(result: ScanDateResult, excluded: list[Excluded], config: ScannerConfig) -> str:
    base = (
        f"Scanned {result.universe_size} securities: {result.candidates} candidates, "
        f"{result.signals_written} signals written, {result.outcomes_recorded} outcomes recorded."
    )
    if not excluded:
        return base
    listed = config.settings.listed_exclusions
    return f"{base} {len(excluded)} security(ies) excluded: " + ", ".join(
        f"{entry.security_id} ({entry.error_type})" for entry in excluded[:listed]
    )
