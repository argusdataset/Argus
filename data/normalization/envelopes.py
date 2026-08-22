"""Provider response envelope absorption.

Module 04 found FMP returns payloads in at least three shapes:

1. a bare array                  ``[{...}, {...}]``
2. a keyed wrapper               ``{"historical": [{...}, ...]}``
3. a bare object (single record) ``{...}``

The adapter's `_rows()` already flattens these at fetch time, so records
reaching this module are typed. This module supplies the same absorption
for the *nested* payloads that ride along inside a typed record's `raw`
and `data` fields, where a wrapper can still appear, and — more
importantly — states the contract a future provider adapter must meet.

**The contract for any provider adapter:** deliver a flat sequence of
typed records, one per observation, with unmodelled provider fields kept
rather than dropped. Envelope shape is the adapter's problem, never the
canonical model's. Nothing downstream of `data.normalization` may branch
on a provider's envelope, field names, or record layout.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

#: Keys FMP uses to wrap a payload. Checked in order; the first present
#: key whose value is a list wins.
WRAPPER_KEYS: tuple[str, ...] = ("historical", "data", "results", "items")


def unwrap_envelope(payload: Any) -> list[dict[str, Any]]:
    """Flatten any of the known envelope shapes to a list of row dicts.

    Anything unrecognised yields an empty list rather than raising: an
    envelope this function cannot read is a *shape* problem for the
    caller to report, not a reason to abort a batch mid-flight. Callers
    that need to distinguish "empty payload" from "unreadable payload"
    check the input themselves — Module 04 already surfaces empty
    responses explicitly via `EmptyReason`.
    """
    if payload is None:
        return []

    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]

    if isinstance(payload, dict):
        for key in WRAPPER_KEYS:
            nested = payload.get(key)
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, dict)]
        # Shape 3: a single record delivered without a wrapper.
        return [payload] if payload else []

    return []


def iter_rows(payload: Any) -> Iterator[dict[str, Any]]:
    """Streaming form of `unwrap_envelope`, for large payloads."""
    yield from unwrap_envelope(payload)
