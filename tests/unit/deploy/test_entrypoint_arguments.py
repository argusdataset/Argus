"""Entrypoints refuse arguments they cannot use, rather than discarding them.

The failure this pins down happened for real. A Railway service was
configured with:

    python -m infra.deploy.migrate && uvicorn infra.deploy.asgi:identity_app ...

Executed without a shell, `&&` and everything after it become argv for the
migration. The old `_ = argv` discarded them, so the migration ran, printed
"schema already at head", exited 0 — and no server was ever started. Every
observable said success; the health check failed five minutes later against a
port nothing was listening on.

The diagnosis was sitting in argv the whole time.
"""

from __future__ import annotations

import pytest

from infra.deploy import migrate, retention, scanner
from infra.deploy.cli import UnexpectedArguments, refuse_arguments

ENTRYPOINTS = [
    ("infra.deploy.migrate", migrate),
    ("infra.deploy.scanner", scanner),
    ("infra.deploy.retention", retention),
]

#: What Railway actually passed, verbatim.
THE_REAL_ONE = [
    "&&",
    "uvicorn",
    "infra.deploy.asgi:identity_app",
    "--factory",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
]


def test_no_arguments_is_the_normal_case_and_passes():
    refuse_arguments("infra.deploy.migrate", [])
    refuse_arguments("infra.deploy.migrate", None)


def test_the_real_misconfiguration_is_refused_with_exit_code_2():
    with pytest.raises(UnexpectedArguments) as refused:
        refuse_arguments("infra.deploy.migrate", THE_REAL_ONE)

    assert refused.value.code == 2


def test_the_refusal_names_the_arguments_verbatim(capsys):
    """Seeing them *is* the diagnosis — so they must be in the message."""
    with pytest.raises(UnexpectedArguments):
        refuse_arguments("infra.deploy.migrate", THE_REAL_ONE)

    err = capsys.readouterr().err
    assert "'&&'" in err
    assert "'uvicorn'" in err
    assert "takes no arguments" in err


def test_a_shell_operator_is_called_out_specifically(capsys):
    """Not cleverness — this is the case that actually happened, and a
    person reading a deploy log should not have to infer it."""
    with pytest.raises(UnexpectedArguments):
        refuse_arguments("infra.deploy.migrate", THE_REAL_ONE)

    err = capsys.readouterr().err
    assert "WITHOUT a shell" in err
    assert "sh -c 'python -m infra.deploy.migrate &&" in err


@pytest.mark.parametrize("operator", ["&&", "||", ";", "|"])
def test_every_shell_operator_triggers_the_hint(capsys, operator):
    with pytest.raises(UnexpectedArguments):
        refuse_arguments("infra.deploy.migrate", [operator, "uvicorn"])

    assert "WITHOUT a shell" in capsys.readouterr().err


def test_a_plain_stray_argument_is_still_refused_without_the_shell_hint(capsys):
    """A typo'd flag is not a shell-form problem, and should not claim to be."""
    with pytest.raises(UnexpectedArguments):
        refuse_arguments("infra.deploy.migrate", ["--head"])

    err = capsys.readouterr().err
    assert "'--head'" in err
    assert "WITHOUT a shell" not in err


@pytest.mark.parametrize(("name", "module"), ENTRYPOINTS, ids=[n for n, _ in ENTRYPOINTS])
def test_every_entrypoint_refuses_before_doing_any_work(name, module, capsys):
    """Before touching a database, before configuring logging.

    A migration that ran and *then* complained would have already done
    the thing the operator could not see it doing.
    """
    with pytest.raises(UnexpectedArguments):
        module.main(THE_REAL_ONE)

    assert name in capsys.readouterr().err


@pytest.mark.parametrize(("name", "module"), ENTRYPOINTS, ids=[n for n, _ in ENTRYPOINTS])
def test_no_entrypoint_silently_discards_argv(name, module):
    """The regression itself: `_ = argv` must not come back."""
    import inspect

    source = inspect.getsource(module.main)
    assert "_ = argv" not in source, f"{name}.main() discards its arguments again"
    assert "refuse_arguments" in source
