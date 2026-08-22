"""Filesystem response cache.

A full-universe historical backfill is tens of thousands of requests. Any
re-run — after a crash, a code change, or a partial failure — must not
pay for them again, both because of the rate limit and because FMP meters
bandwidth on a trailing-30-day window.

Two properties matter for correctness rather than speed:

- **The original fetch timestamp is preserved.** A cache hit returns the
  time the data was really observed, not the time it was read back.
  Module 05 maps that to `ingestion_time`; refreshing it on every read
  would misstate the point-in-time record.
- **Writes are atomic.** Entries are written to a temporary file and
  renamed, so a process killed mid-write leaves either the old entry or
  no entry, never a truncated one that parses as valid JSON.

Historical bars never change, so those entries have no expiry. Calendars,
news, and listings do change; `Endpoint.immutable` marks which is which
and the client passes a max age accordingly.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A cached response body plus the time it was originally fetched."""

    body: Any
    fetched_at: datetime
    status_code: int


def cache_key(endpoint_name: str, url_path: str, params: dict[str, Any]) -> str:
    """Stable key for one request.

    The API key is never part of the key — it is a credential, not a
    request parameter, and including it would both fragment the cache
    across key rotations and write the secret into filenames.
    """
    payload = json.dumps(
        {"endpoint": endpoint_name, "path": url_path, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResponseCache:
    """Content-addressed cache of FMP responses on the local filesystem."""

    def __init__(self, directory: str | Path, *, enabled: bool = True) -> None:
        self._directory = Path(directory)
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _path_for(self, key: str) -> Path:
        # Two-level fan-out: a flat directory with tens of thousands of
        # entries degrades badly on most filesystems.
        return self._directory / key[:2] / key[2:4] / f"{key}.json"

    def get(self, key: str, *, max_age: timedelta | None = None) -> CacheEntry | None:
        """Return the entry, or None if absent, unreadable, or too old."""
        if not self._enabled:
            return None

        path = self._path_for(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A missing or corrupt entry is a cache miss, never a failure:
            # the caller can always re-fetch.
            return None

        try:
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            entry = CacheEntry(
                body=payload["body"],
                fetched_at=fetched_at,
                status_code=payload.get("status_code", 200),
            )
        except (KeyError, TypeError, ValueError):
            return None

        if max_age is not None and datetime.now(UTC) - entry.fetched_at > max_age:
            return None
        return entry

    def set(self, key: str, entry: CacheEntry) -> None:
        """Write an entry atomically."""
        if not self._enabled:
            return

        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "body": entry.body,
            "fetched_at": entry.fetched_at.isoformat(),
            "status_code": entry.status_code,
        }

        # Same directory as the target, so the rename stays on one
        # filesystem and is therefore atomic.
        handle, temp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(payload, file)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
