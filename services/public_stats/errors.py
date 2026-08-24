"""Errors, in Module 19's envelope rather than a second one.

`error_payload` is imported from `services.terminal.errors` and used
directly. It is a pure function over three arguments with nothing
Terminal-specific in it, so it met the brief's "use it if it's structured
for reuse" test.

**Flagged rather than fixed:** `error_payload` and the `TerminalError`
shape now serve two services and live in one of them, which means
`services/public_stats` imports from `services/terminal` for a reason
that has nothing to do with the Terminal. The right home is a shared
`services/errors.py` with an `ApiError` base that both subclass. Moving
it means editing Module 19, which this module's boundaries forbid, so it
is reported instead — see the module report. Importing was chosen over
duplicating because two envelopes that drift is a worse outcome than one
import in the wrong direction.
"""

from __future__ import annotations

from typing import Any

from services.terminal.errors import error_payload

__all__ = [
    "CHART_NOT_FOUND",
    "RELEASE_NOT_FOUND",
    "REVIEW_REFUSED",
    "STATISTICS_WITHDRAWN",
    "STATISTICS_UNAVAILABLE",
    "PublicStatsError",
    "error_payload",
]

#: No such chart. The set is closed and small — see `aggregates.py`.
CHART_NOT_FOUND = "CHART_NOT_FOUND"
#: A release window nobody has queued for review.
RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
#: A review action that would not have been a review.
REVIEW_REFUSED = "REVIEW_REFUSED"
#: Nothing has been published yet — no approved run, no approved window,
#: or no refresh has run. A 200-with-empty is wrong here: "we have not
#: published statistics" is a different fact from "our statistics are
#: zero", and only one of them is true.
STATISTICS_UNAVAILABLE = "STATISTICS_UNAVAILABLE"
#: Something that was published has since been withdrawn, and the stored
#: payload still contains it. Refused rather than served — the one error
#: in this module that exists to protect a reader rather than to inform
#: a caller.
STATISTICS_WITHDRAWN = "STATISTICS_WITHDRAWN"


class PublicStatsError(Exception):
    """An error with a stable code, carried to the HTTP layer intact."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}

    def payload(self) -> dict[str, Any]:
        return error_payload(self.code, self.message, self.detail)
