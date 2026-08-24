"""Classifying what went wrong, because the response depends on the answer.

Module 17's batch replay has one failure policy — record FAILED and
re-raise — and it is the right one there: a human started the run, is
watching it, and will re-run it. An unattended daily scanner cannot use
that policy, not because it is too harsh but because it treats three
genuinely different situations as one.

The three, and why each needs its own response:

**Transient.** The database was restarting, the connection dropped, the
pool timed out. Nothing is wrong with the data or the code; the same call
a few seconds later succeeds. Re-raising here converts a five-second blip
into a missed trading day.

**Isolatable.** One security's data is malformed in a way that throws.
Failing the whole day for one bad name means one broken row in a
ten-thousand-name universe silently stops all of ARGUS — and since the bad
row will still be there tomorrow, it stops it permanently.

**Permanent.** A version mismatch, a schema problem, a bug. Retrying will
not help and neither will excluding a security. This is the one that wants
a person, and the value of the other two categories is that it stays rare
enough to be worth reading.

## Classification is conservative in one direction

Anything not positively identified as transient is treated as
non-transient. A misclassified transient error costs a missed day that the
next scheduled run picks up; a misclassified permanent error costs a
retry loop that hides a real problem behind repeated failed attempts. The
second is worse, so the default is to not retry.

`IntegrityError` deserves specific mention: it is a `DBAPIError`
subclass, so a naive "any database error is transient" rule would retry
it forever. A constraint violation is a statement about the data and will
be violated identically on every attempt.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import exc as sa_exc

from core.live_scanner.config import ScannerConfig

__all__ = ["FailureClass", "classify_failure", "describe_failure"]


class FailureClass(StrEnum):
    """What kind of wrong this is, which decides what happens next."""

    TRANSIENT = "TRANSIENT"
    ISOLATABLE = "ISOLATABLE"
    PERMANENT = "PERMANENT"


#: Database errors that mean "the connection or server had a moment".
#: Deliberately a tuple of specific classes rather than `DBAPIError`:
#: `IntegrityError`, `DataError` and `ProgrammingError` are all
#: `DBAPIError` subclasses and none of them gets better on a retry.
_TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.InternalError,
    sa_exc.TimeoutError,
    sa_exc.DisconnectionError,
    ConnectionError,
    TimeoutError,
)

#: Errors that never get better and never belong to one security.
_PERMANENT_TYPES: tuple[type[BaseException], ...] = (
    sa_exc.IntegrityError,
    sa_exc.ProgrammingError,
    sa_exc.InvalidRequestError,
)


def classify_failure(error: BaseException) -> FailureClass:
    """Which of the three kinds of wrong this is.

    Checked most-specific first, because the permanent set and the
    transient set overlap in the type hierarchy — `IntegrityError` is a
    `DBAPIError`, and `StatementError` sits above several of both.
    """
    from core.model_validation_evaluation.validation.versions import VersionMismatch

    if isinstance(error, VersionMismatch):
        # Correction 3's refusal. Retrying is pointless and excluding a
        # security is nonsense: the fix is to publish a version or check
        # out different code, and both are a person's job.
        return FailureClass.PERMANENT

    if isinstance(error, _PERMANENT_TYPES):
        return FailureClass.PERMANENT

    if isinstance(error, _TRANSIENT_TYPES):
        # A dead connection can also arrive wrapped as a DBAPIError with
        # the flag set, which is how SQLAlchemy reports a mid-statement
        # disconnect.
        return FailureClass.TRANSIENT

    if isinstance(error, sa_exc.DBAPIError) and getattr(error, "connection_invalidated", False):
        return FailureClass.TRANSIENT

    # Everything else: a computation threw. It may well be one security's
    # data, so it is worth *asking* — `scanner.py` probes per security and
    # falls back to PERMANENT when nothing is attributable.
    return FailureClass.ISOLATABLE


def describe_failure(error: BaseException, config: ScannerConfig | None = None) -> dict[str, str]:
    """The structured form a Module 23 observability layer would read.

    The exception's own text, its type, and the classification that
    decided what happened next — so a stored failure explains not just
    what broke but why the scanner responded the way it did.
    """
    config = config or ScannerConfig()
    return {
        "type": type(error).__name__,
        "message": str(error)[: config.settings.message_limit],
        "classification": classify_failure(error).value,
    }
