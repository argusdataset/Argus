"""A service that cannot start must say so. No database — the failure is the point.

The gap this closes was found the expensive way: an `identity` container on
Railway that produced no uvicorn line, no traceback, and no error — just
silence, then a failed health check five minutes later. From the outside,
"the factory raised" and "the factory was never called" looked identical,
and the logs could not tell them apart.

So two properties are pinned here:

1. Entering the factory is announced *before* anything that could fail,
   including `configure_logging` itself. Its absence is then evidence.
2. Any failure during construction is reported on two independent
   channels, and then re-raised so the deploy still fails.
"""

from __future__ import annotations

import pytest

from infra.deploy import asgi


@pytest.fixture
def broken(monkeypatch):
    """Make `build_service` fail the way a real misconfiguration does."""

    def _explode(name: str, **_kwargs) -> None:
        raise RuntimeError(f"deliberate failure building {name}")

    monkeypatch.setattr(asgi, "build_service", _explode)


def test_entering_the_factory_is_announced_before_anything_can_fail(capsys, broken):
    """The line that distinguishes 'died inside' from 'never called'.

    A bare `print` to stderr, not a log record: it must not depend on
    `configure_logging` having worked, because a logging failure is one
    of the things it exists to make visible.
    """
    with pytest.raises(RuntimeError):
        asgi.identity_app()

    assert asgi._ENTERED in capsys.readouterr().err


def test_a_construction_failure_is_reported_and_then_re_raised(capsys, broken):
    """Never swallowed. A logged failure that returned normally would
    leave uvicorn serving an app that was never built."""
    with pytest.raises(RuntimeError, match="deliberate failure building identity"):
        asgi.identity_app()

    err = capsys.readouterr().err
    assert "FAILED to build service" in err
    assert "name=identity" in err
    assert "error_type=RuntimeError" in err


def test_the_traceback_reaches_stderr_not_just_the_message(capsys, broken):
    """A message says what broke; a traceback says where."""
    with pytest.raises(RuntimeError):
        asgi.identity_app()

    assert "Traceback (most recent call last)" in capsys.readouterr().err


def test_the_failure_is_also_a_structured_record(broken, monkeypatch):
    """So it is queryable beside every other ARGUS log, by `event`.

    Captured at the logger rather than through `caplog`, because
    `configure_logging(force=True)` removes pytest's own root handler —
    which is Module 23 claiming ownership of logging, working exactly as
    designed, and incidentally making `caplog` blind here.
    """
    captured: dict = {}

    def _capture(message, *_args, **kwargs):
        captured["message"] = message
        captured["extra"] = kwargs.get("extra", {})

    monkeypatch.setattr(asgi._log, "exception", _capture)

    with pytest.raises(RuntimeError):
        asgi.identity_app()

    assert captured["extra"]["event"] == "service_startup_failed"
    assert captured["extra"]["service"] == "identity"
    assert captured["extra"]["error_type"] == "RuntimeError"


def test_a_broken_logger_still_leaves_the_stderr_report(capsys, broken, monkeypatch):
    """The case the two channels exist for.

    If `configure_logging` is itself what failed, the structured record
    is exactly what cannot be written — so losing it must not cost the
    traceback too.
    """

    def _no_logging(*_args, **_kwargs) -> None:
        raise RuntimeError("logging is broken")

    monkeypatch.setattr(asgi._log, "exception", _no_logging)

    with pytest.raises(RuntimeError):
        asgi.identity_app()

    err = capsys.readouterr().err
    assert "FAILED to build service" in err
    assert "Traceback (most recent call last)" in err


@pytest.mark.parametrize(
    "factory",
    ["terminal_app", "public_stats_app", "intelligence_app", "identity_app", "health_app"],
)
def test_every_service_factory_reports_rather_than_dying_quietly(capsys, broken, factory):
    """Identity is where this was found; it is not where it could happen."""
    with pytest.raises(RuntimeError):
        getattr(asgi, factory)()

    err = capsys.readouterr().err
    assert asgi._ENTERED in err
    assert "FAILED to build service" in err
