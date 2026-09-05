"""The Terminal's error codes. The envelope itself lives in `services/shared/`.

## Where the envelope went

`error_payload` and the error base class started here, because the
Terminal was the first module with a consumer outside ARGUS. Module 20
reused them by importing from this file and flagged that as the wrong
shape; Module 21 moved them to `services/shared/`. This module now
imports what it once defined, which is the right direction for a
convention with three consumers.

## Why the codes exist

This is the first module whose caller cannot read the modules behind it.
A consumer handling a failure has two questions — *can I retry* and *is
this my fault* — and an HTTP status alone answers neither well. 404 is
"this ticker does not exist" and also "this ticker exists but ARGUS has
never ingested it", which want different messages in a UI.

So every error carries a stable `code` string. The status tells a proxy
what to do; the code tells the client what to say.

## The envelope is the same shape every time

```json
{"error": {"code": "SECURITY_NOT_FOUND",
           "message": "No security is trading as 'ZZZZ'.",
           "detail": {"ticker": "ZZZZ"}}}
```

`message` is written for a human reading a log or a toast. `detail` is
structured, optional, and never required for the client to function —
a consumer that only ever reads `code` is using this correctly.

## What is not an error

An empty result is not an error. A security with no ingested fundamentals
returns 200 with the statements block explicitly marked unavailable and
the reason named — see `schemas.py`. Modelling "we have nothing for this"
as a 404 would make a consumer unable to distinguish it from a bad
ticker, which is the same conflation Module 18's report warned about for
scan availability.
"""

from __future__ import annotations

from services.shared.errors import ApiError, error_payload

__all__ = [
    "IDENTITY_REQUIRED",
    "IDENTITY_UNAVAILABLE",
    "INVALID_REQUEST",
    "NOT_FOUND",
    "SECURITY_NOT_FOUND",
    "invalid_indicator",
    "WATCHLIST_LIMIT_REACHED",
    "WATCHLIST_NAME_TAKEN",
    "WATCHLIST_NOT_FOUND",
    "UNKNOWN_INDICATOR",
    "TerminalError",
    "error_payload",
]

#: The ticker resolved to nothing. Distinct from "resolved, but ARGUS has
#: no data for it", which is a 200 with an unavailable block.
SECURITY_NOT_FOUND = "SECURITY_NOT_FOUND"
#: A watchlist that does not exist, or is not this user's. Deliberately
#: the same code for both — see `watchlists.py` on why a caller must not
#: be able to distinguish them.
WATCHLIST_NOT_FOUND = "WATCHLIST_NOT_FOUND"
WATCHLIST_NAME_TAKEN = "WATCHLIST_NAME_TAKEN"
WATCHLIST_LIMIT_REACHED = "WATCHLIST_LIMIT_REACHED"
#: No identity was supplied for a user-scoped endpoint.
IDENTITY_REQUIRED = "IDENTITY_REQUIRED"
#: Identity was supplied but this deployment has no way to verify it —
#: the stub is off and Module 22 is not built. A 501, not a 401: the
#: caller did nothing wrong, the server cannot answer yet.
IDENTITY_UNAVAILABLE = "IDENTITY_UNAVAILABLE"
#: A technical indicator FMP does not expose. A 400 rather than an empty
#: series, because "no such indicator" and "this one has not been
#: ingested yet" are different facts and an empty list would state the
#: second when the first is true — the same conflation this module's
#: docstring warns about for 404.
UNKNOWN_INDICATOR = "UNKNOWN_INDICATOR"
INVALID_REQUEST = "INVALID_REQUEST"
NOT_FOUND = "NOT_FOUND"


class TerminalError(ApiError):
    """A Terminal error. The envelope is `services.shared.errors.ApiError`.

    Subclassed rather than aliased so `app.py`'s handler catches the
    Terminal's errors and not another service's — two services sharing an
    exception class would mean one service's handler silently answering
    for the other.
    """


def security_not_found(ticker: str) -> TerminalError:
    return TerminalError(
        SECURITY_NOT_FOUND,
        f"No security is trading as {ticker!r}.",
        status=404,
        detail={"ticker": ticker},
    )


def invalid_indicator(indicator: str, allowed: tuple[str, ...]) -> TerminalError:
    """An indicator name nothing can ever satisfy.

    `detail` carries the full allowed list rather than only the rejected
    name: a client building a selector should be able to populate it from
    one failed request instead of hard-coding a list that will drift.
    """
    return TerminalError(
        UNKNOWN_INDICATOR,
        f"{indicator!r} is not an indicator the provider computes.",
        status=400,
        detail={"indicator": indicator, "allowed": list(allowed)},
    )
