"""Conventions every ARGUS service shares.

Three things ended up here, and the route they took is the reason this
package exists rather than being a tidy-up.

Module 19 built an error envelope and an `Unavailable` shape for the
Terminal, because it was the first module with a consumer outside ARGUS.
Module 20 needed both, found them structured for reuse, and imported them
from `services/terminal/` — then flagged in its report that
`services/public_stats` now depended on the Terminal for reasons that had
nothing to do with the Terminal, and that the right home was a shared
location before a third consumer arrived.

Module 21 is that third consumer. So the conventions moved here first, and
Modules 19 and 20 were updated to import from here — a change to their
import lines and nothing else.

## What is shared and what is not

Shared: the **envelope**, not the errors. `ApiError` and `error_payload`
fix the shape `{"error": {"code", "message", "detail"}}`; each service
defines its own codes, because a code is a statement about that service's
domain and a shared enumeration of every code in ARGUS would be a list
nobody could keep meaningful.

Shared: `Unavailable`, `Freshness` and `Provenance` — the three response
blocks that answer "why is this missing", "how old is this" and "where
did this come from". Those questions are identical in every service, and
three different answers to them would be exactly the drift this package
prevents.

Not shared: anything a service computes, serves or decides.
"""

from services.shared.errors import ApiError, error_payload
from services.shared.schemas import Freshness, Provenance, Unavailable

__all__ = [
    "ApiError",
    "Freshness",
    "Provenance",
    "Unavailable",
    "error_payload",
]
