"""The Intelligence API's error codes. The envelope lives in `services/shared/`.

Deliberately few codes, and the reason is this module's central property:
**almost nothing here is an error.** A candidate ARGUS cannot score, a
security with no similarity evidence, a risk input that is honestly
undetermined — those are all 200 responses carrying `Unavailable` and a
reason. They are the majority of what this module serves, and modelling
them as failures would make a consumer unable to tell "ARGUS looked and
cannot say" from "your request was wrong".

Only two things genuinely go wrong here: a security that does not exist,
and a setup that has not concluded being asked why it failed.
"""

from __future__ import annotations

from services.shared.errors import ApiError

__all__ = [
    "NOT_CONCLUDED",
    "SECURITY_NOT_FOUND",
    "SETUP_NOT_FOUND",
    "UNKNOWN_WATCHLIST",
    "IntelligenceError",
]

#: The ticker resolved to nothing. Distinct from "resolved, but ARGUS has
#: nothing to say about it", which is a 200 with `Unavailable` blocks.
SECURITY_NOT_FOUND = "SECURITY_NOT_FOUND"
#: No such setup, or none for this security.
SETUP_NOT_FOUND = "SETUP_NOT_FOUND"
#: A setup asked "why did this fail" before it finished. Not a 404: the
#: setup exists and is fine, it simply has no outcome yet.
NOT_CONCLUDED = "NOT_CONCLUDED"
#: Not one of the four. The set is closed by Module 10.
UNKNOWN_WATCHLIST = "UNKNOWN_WATCHLIST"


class IntelligenceError(ApiError):
    """An Intelligence API error. Envelope from `services.shared.errors`.

    Subclassed rather than aliased so this service's handler catches its
    own errors and not another service's.
    """
