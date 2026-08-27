"""Structured logging: the convention, the formatter, and the scrubber.

Before this module ARGUS had exactly one logger — `argus.identity.seam`,
added by Module 22 — and no formatting convention at all. That is worth
saying plainly, because it changes what the Alembic incident actually
meant and it is the reason this file leads with a convention rather than
a rewrite: there was almost nothing to rewrite.

## The convention

One event per line, JSON, on stderr. Every record carries the same
skeleton:

```json
{"time": "...", "level": "WARNING", "logger": "argus.identity.seam",
 "event": "authentication_bypass", "user_id": "...", "mechanism": "stub"}
```

`event` is a stable snake_case name — the thing a query groups by.
Message text is for a person reading one line; `event` is for the tool
reading a million. Everything else is a flat structured field.

Call it like this:

```python
log = get_logger("argus.live_scanner")
log.warning("scan failed", extra=fields(event="scan_failed", scan_date=...))
```

`fields()` exists so the `extra=` dict is built one way. Passing a bare
dict works and is not stopped; passing it through `fields()` is what puts
the values through the scrubber.

## The scrubber redacts; Module 22's raises. That difference is deliberate.

Module 22's `audit.record` **raises** on a credential-shaped key, because
`audit_log` is append-only: a secret written there cannot be deleted, only
discovered, so refusing to write is the only safe answer.

A log line is a different medium with a different trade. Raising here
would mean an observability call taking down the request it was observing
— the monitoring crashing the thing it monitors, which is the classic way
observability makes a system less reliable rather than more. So a
credential-shaped field is **replaced with `"[redacted]"`** and the record
gains `redacted: ["field_name"]`, so the redaction is itself visible
rather than silent.

Both behaviours are correct for their medium. The shared part is the key
list, which lives here and which Module 22's is a stricter sibling of.

## `configure_logging` claims ownership, and Alembic respects it

`fileConfig` does two things beyond disabling loggers: it resets the root
level and **replaces the root handlers**. So even with
`disable_existing_loggers=False`, a migration run in-process after this
function would swap the JSON handler for Alembic's plain stderr one and
drop the level to WARNING. Module 22 closed the first hole; this closes
the rest, by having `env.py` skip `fileConfig` entirely once an
application has said it owns logging.

A standalone `alembic upgrade head` never calls `configure_logging`, so it
keeps Alembic's console output exactly as before.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "CREDENTIAL_KEY_PARTS",
    "REDACTED",
    "JsonFormatter",
    "configure_logging",
    "fields",
    "get_logger",
    "logging_is_configured",
    "reset_logging",
    "scrub",
]

#: Substrings that mark a field as credential material. A superset of the
#: names Module 22 refuses for `audit_log`, kept here because a log line
#: reaches more places than an audit row does.
CREDENTIAL_KEY_PARTS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "otp",
        "mfa_code",
        "authorization",
        "cookie",
        "api_key",
        "apikey",
        "private",
        "session_key",
        "bearer",
    }
)

REDACTED = "[redacted]"

#: The keys `logging.LogRecord` already owns. A structured field colliding
#: with one of these would be silently dropped by the stdlib, so they are
#: excluded from the payload rather than fought over.
_RESERVED: frozenset[str] = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)))

#: Set by `configure_logging`. Read by the Alembic environment.
_configured = False


def logging_is_configured() -> bool:
    """Whether an application has taken ownership of logging configuration.

    `infra/db/migrations/env.py` reads this and skips `fileConfig` when it
    is true, so an in-process migration cannot replace a configured
    application's handlers. See the module docstring.
    """
    return _configured


def configure_logging(
    *,
    level: int | str = logging.INFO,
    stream: Any = None,
    force: bool = True,
) -> logging.Handler:
    """Install the JSON handler on the root logger and claim ownership.

    Returns the handler so a caller can hold onto it — a test asserting
    that a migration did not replace it needs the identity, not just the
    shape.
    """
    global _configured

    root = logging.getLogger()
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.set_name("argus-json")

    if force:
        for existing in list(root.handlers):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    _configured = True
    return handler


def reset_logging() -> None:
    """Release ownership. For tests, so one does not leak into the next."""
    global _configured

    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == "argus-json":
            root.removeHandler(handler)
    _configured = False


def get_logger(name: str) -> logging.Logger:
    """A logger under the `argus.` namespace.

    Namespaced so a deployment can raise or lower ARGUS's verbosity
    without touching the libraries it depends on — the reason a project
    uses a name hierarchy at all.
    """
    if not name.startswith("argus"):
        name = f"argus.{name}"
    return logging.getLogger(name)


def fields(*, event: str, **values: Any) -> dict[str, Any]:
    """Build the `extra=` payload for one log call, scrubbed.

    `event` is required and is the stable name a query groups by. A log
    line whose only identity is its English message is a line nobody can
    count.
    """
    payload = scrub(values)
    payload["event"] = event
    return payload


def scrub(values: dict[str, Any]) -> dict[str, Any]:
    """Replace credential-shaped values, and say which were replaced.

    Redacts rather than raises — see the module docstring on why the trade
    differs from Module 22's. Recurses into nested dicts, because a secret
    one level down is still a secret.
    """
    redacted: list[str] = []
    cleaned = _scrub(values, redacted, prefix="")
    if redacted:
        cleaned["redacted"] = sorted(redacted)
    return cleaned


def _scrub(values: dict[str, Any], redacted: list[str], *, prefix: str) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        name = str(key)
        path = f"{prefix}{name}"
        if any(part in name.lower() for part in CREDENTIAL_KEY_PARTS):
            cleaned[name] = REDACTED
            redacted.append(path)
            continue
        if isinstance(value, dict):
            cleaned[name] = _scrub(value, redacted, prefix=f"{path}.")
            continue
        cleaned[name] = value
    return cleaned


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Never raises on an unserialisable value.

    `default=str` rather than a strict encoder: a formatter that throws
    turns a log call into an application error, which is the failure mode
    an observability layer must not have. A UUID or a datetime that
    arrives as a field becomes its string form and the line still ships.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.name.rsplit(".", 1)[-1]),
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key in _RESERVED or key in payload:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, sort_keys=False)
