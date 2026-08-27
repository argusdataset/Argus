"""The public page's error codes. The envelope lives in `services/shared/`.

Module 20 originally imported `error_payload` from
`services/terminal/errors.py` and flagged that as the wrong shape: the
public stats service depended on the Terminal for a reason that had
nothing to do with the Terminal. Module 21 moved the envelope to
`services/shared/`, and this file now imports from there.

The codes stay here. A code is a statement about this service's domain,
and `STATISTICS_WITHDRAWN` has nothing to say to the Terminal.
"""

from __future__ import annotations

from services.shared.errors import ApiError, error_payload

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


class PublicStatsError(ApiError):
    """A public-stats error. The envelope is `services.shared.errors.ApiError`.

    Subclassed rather than aliased so this service's handler catches its
    own errors and not another's.
    """
