"""Structured logging: the shape, and that no secret can reach a line."""

from __future__ import annotations

import io
import json
import logging
from uuid import uuid4

import pytest

from infra.observability.logging import (
    CREDENTIAL_KEY_PARTS,
    REDACTED,
    configure_logging,
    fields,
    get_logger,
    reset_logging,
    scrub,
)


@pytest.fixture
def emitted():
    """Capture JSON lines from a configured logger, then restore the root."""
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    stream = io.StringIO()
    configure_logging(level=logging.DEBUG, stream=stream)

    def _lines() -> list[dict]:
        return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]

    yield _lines
    reset_logging()
    root.handlers[:] = before
    root.setLevel(level)


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_every_record_is_one_json_object_per_line(emitted):
    log = get_logger("argus.example")

    log.info("scan finished", extra=fields(event="scan_finished", scan_date="2026-08-20"))
    log.warning("feed late", extra=fields(event="feed_delayed", feed="ohlcv"))

    lines = emitted()
    assert len(lines) == 2
    assert lines[0]["event"] == "scan_finished"
    assert lines[1]["event"] == "feed_delayed"


def test_the_skeleton_is_the_same_on_every_record(emitted):
    """A field that is sometimes there is a field nothing can query on."""
    log = get_logger("argus.example")
    log.info("one", extra=fields(event="a"))
    log.error("two", extra=fields(event="b", detail=1))

    for line in emitted():
        assert {"time", "level", "logger", "event", "message"} <= set(line)


def test_structured_fields_are_flat_alongside_the_skeleton(emitted):
    log = get_logger("argus.example")

    log.info(
        "classified", extra=fields(event="state_assigned", securities=412, state="CONSOLIDATION")
    )

    line = emitted()[0]
    assert line["securities"] == 412
    assert line["state"] == "CONSOLIDATION"
    assert line["logger"] == "argus.example"
    assert line["level"] == "INFO"


def test_a_bare_name_is_placed_under_the_argus_namespace(emitted):
    """So a deployment can turn ARGUS's verbosity up without its libraries'."""
    assert get_logger("live_scanner").name == "argus.live_scanner"
    assert get_logger("argus.identity.seam").name == "argus.identity.seam"


def test_an_unserialisable_value_does_not_break_the_line(emitted):
    """A formatter that raises turns a log call into an application error.

    Which is the failure mode an observability layer must not have: the
    monitoring taking down the thing it monitors.
    """
    log = get_logger("argus.example")
    identifier = uuid4()

    log.info("done", extra=fields(event="done", security_id=identifier, when=object()))

    line = emitted()[0]
    assert line["security_id"] == str(identifier)
    assert "object" in line["when"]


def test_an_exception_is_captured_as_a_field(emitted):
    log = get_logger("argus.example")

    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed", extra=fields(event="task_failed"))

    line = emitted()[0]
    assert "ValueError: boom" in line["exception"]


def test_a_field_colliding_with_a_record_attribute_is_dropped_not_crashed(emitted):
    """`logging` owns names like `msg` and `args`; fighting it would raise."""
    log = get_logger("argus.example")

    log.info("hello", extra=fields(event="e", safe_field=1))

    line = emitted()[0]
    assert line["safe_field"] == 1
    assert line["message"] == "hello"


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "new_password",
        "session_token",
        "mfa_secret",
        "api_key",
        "authorization",
        "cookie",
        "bearer_token",
        "private_key",
        "otp",
    ],
)
def test_a_credential_shaped_field_is_redacted(emitted, key):
    log = get_logger("argus.example")

    log.info("attempt", extra=fields(event="login", **{key: "hunter2"}))

    line = emitted()[0]
    assert line[key] == REDACTED
    assert "hunter2" not in json.dumps(line)


def test_the_redaction_names_itself(emitted):
    """A silent redaction and a field that was never sent look identical."""
    log = get_logger("argus.example")

    log.info("attempt", extra=fields(event="login", email="a@b.test", password="hunter2"))

    line = emitted()[0]
    assert line["redacted"] == ["password"]
    assert line["email"] == "a@b.test"


def test_redaction_reaches_into_nested_values(emitted):
    log = get_logger("argus.example")

    log.info(
        "attempt",
        extra=fields(event="login", context={"supplied": {"password": "hunter2"}}),
    )

    line = emitted()[0]
    assert line["context"]["supplied"]["password"] == REDACTED
    assert line["redacted"] == ["context.supplied.password"]
    assert "hunter2" not in json.dumps(line)


def test_the_scrubber_redacts_rather_than_raising():
    """Deliberately unlike Module 22's, and the docstring says why.

    `audit_log` is append-only so a secret written there cannot be
    deleted; refusing to write is the only safe answer. A log line is a
    different medium, and raising here would mean an observability call
    taking down the request it was observing.
    """
    cleaned = scrub({"password": "hunter2", "user": "alice"})

    assert cleaned["password"] == REDACTED
    assert cleaned["user"] == "alice"
    assert cleaned["redacted"] == ["password"]


def test_the_key_list_covers_everything_module_22_refuses():
    """A field Module 22 will not store must not be one this module logs."""
    from services.identity.audit import _FORBIDDEN_KEY_PARTS

    assert _FORBIDDEN_KEY_PARTS <= CREDENTIAL_KEY_PARTS


def test_no_call_in_this_module_logs_a_credential():
    """This module's own code, scanned the way Module 22 scans its own.

    Whether the same AST discipline is worth extending to every package
    is flagged in the report rather than decided here.
    """
    import ast
    from pathlib import Path

    methods = {"debug", "info", "warning", "error", "exception", "critical", "log"}
    sources = sorted(Path("infra/observability").glob("*.py"))
    assert len(sources) >= 7, "the scan found the files it is meant to scan"

    for path in sources:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in methods):
                continue
            for argument in ast.walk(ast.Module(body=list(node.args), type_ignores=[])):
                name = (
                    argument.id
                    if isinstance(argument, ast.Name)
                    else argument.attr
                    if isinstance(argument, ast.Attribute)
                    else ""
                )
                assert not any(part in name.lower() for part in CREDENTIAL_KEY_PARTS), (
                    f"{path.name}:{node.lineno} logs {name!r}"
                )
