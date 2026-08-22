"""The result type every PIT query returns.

**No implicit "current" fallback.** If nothing satisfies the as-of
constraint, a caller must be told that explicitly rather than receiving
`None` (indistinguishable from "the field is null") or, worse, the most
recent row regardless of date (a silent leak dressed up as a convenience).
`AsOfResult` makes "not yet available as of this date" a first-class,
checkable outcome instead of a special value someone has to remember to
test for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

T = TypeVar("T")


class MissReason(StrEnum):
    """Why an as-of query found nothing, for a caller that wants to react differently."""

    #: No row for this logical record exists with availability_time <= as_of.
    #: Could mean the record does not exist yet, or exists but is not yet
    #: knowable as of the requested date — the two are indistinguishable
    #: from inside the database, which is itself the point: ARGUS must not
    #: claim to know which one it is.
    NOT_YET_AVAILABLE = "not_yet_available"
    #: The logical record (e.g. this security_id + fiscal_period) has no
    #: row at all, at any availability_time. Distinguished from
    #: NOT_YET_AVAILABLE when the caller can tell the two apart (e.g. a
    #: later, unconstrained lookup confirms nothing was ever ingested).
    NEVER_INGESTED = "never_ingested"
    #: The as-of date falls outside a dated interval (universe membership):
    #: the security was not listed, or had already delisted, at that date.
    OUTSIDE_INTERVAL = "outside_interval"


@dataclass(frozen=True, slots=True)
class AsOfResult(Generic[T]):
    """The outcome of one point-in-time query.

    Always check `found` (or use `unwrap()`) before reading `value` — a
    result with `found=False` has `value=None` and `reason` set, and
    reading `value` without checking is exactly the "implicit current
    fallback" bug this module exists to make impossible.
    """

    found: bool
    as_of: datetime
    value: T | None = None
    reason: MissReason | None = None

    def __bool__(self) -> bool:
        return self.found

    @classmethod
    def hit(cls, value: T, *, as_of: datetime) -> AsOfResult[T]:
        return cls(found=True, as_of=as_of, value=value)

    @classmethod
    def miss(cls, reason: MissReason, *, as_of: datetime) -> AsOfResult[T]:
        return cls(found=False, as_of=as_of, reason=reason)

    def unwrap(self) -> T:
        """The value, or raise. For a caller that has already checked `found`."""
        if not self.found:
            raise LookupError(
                f"No data available as of {self.as_of.isoformat()} "
                f"(reason={self.reason.value if self.reason else 'unknown'})."
            )
        assert self.value is not None
        return self.value

    def unwrap_or(self, default: T) -> T:
        return self.value if self.found else default
