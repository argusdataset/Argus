"""One error envelope for every ARGUS service.

## Why the envelope is shared and the codes are not

Every service in ARGUS answers to a consumer that cannot read the modules
behind it, and such a consumer has two questions on a failure — *can I
retry* and *is this my fault*. An HTTP status answers neither well: 404
is "no such ticker" and also "that watchlist is not yours", which want
different messages in a UI.

So every error carries a stable `code` string. The status tells a proxy
what to do; the code tells the client what to say.

The **codes** stay with their services. A code is a statement about one
service's domain, and a shared enumeration of every code in ARGUS would be
a list nobody could keep meaningful — the Terminal's `SECURITY_NOT_FOUND`
and the public page's `STATISTICS_WITHDRAWN` have nothing to say to each
other.

## What is not an error

An empty result. A security with no ingested fundamentals, a watchlist
with nobody on it, a candidate ARGUS declined to score — all of those are
200 responses carrying `Unavailable` and a reason. Modelling them as
errors would make a consumer unable to tell them from a bad request,
which is the conflation this whole convention exists to prevent.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ApiError", "error_payload"]


def error_payload(code: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """The envelope. One function, so every error in ARGUS matches.

    `message` is written for a human reading a log or a toast. `detail` is
    structured, optional, and never required for the client to function —
    a consumer that only ever reads `code` is using this correctly.
    """
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


class ApiError(Exception):
    """An error with a stable code, carried to the HTTP layer intact.

    Raised by service functions, which know nothing about HTTP. Each
    service installs one handler that turns these into responses, so a
    service module never imports a status code and the envelope is built
    in exactly one place.

    Subclassed per service — `TerminalError`, `PublicStatsError`,
    `IntelligenceError` — so a handler registered for one service's errors
    does not silently catch another's.
    """

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
