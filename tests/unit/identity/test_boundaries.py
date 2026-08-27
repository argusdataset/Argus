"""The boundaries Module 22 introduces, held by the parse tree.

AST, not text — Module 20's near-miss is the reason. A source-position or
substring test can pass against code broken in a way that preserves the
text, and every check here is one where a false pass means a security
property is quietly gone.

Four boundaries:

1. **This service never accepts the identity stub.** Changing your own
   password through an authentication bypass would make the bypass a full
   account takeover rather than a development convenience.
2. **Credentials do not become response fields.** Checked against Pydantic
   `model_fields`, not source text, with exactly one documented exception.
3. **Only `accounts.log_in` issues a session.** `sessions.create` does not
   authenticate; calling it anywhere else hands a credential to somebody
   who has proven nothing.
4. **No plaintext credential reaches a logger.** A logging call taking a
   variable named like a password is the classic way a secret ends up in
   an aggregator.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path("services/identity")
SOURCES = sorted(PACKAGE.glob("*.py"))

#: Names that would mean this service trusts the header stub.
STUB_NAMES: frozenset[str] = frozenset(
    {"current_user_id", "stub_user_exists", "USER_HEADER", "resolve_identity"}
)

#: Key fragments naming credential material.
CREDENTIAL_PARTS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "token",
        "secret",
        "credential",
        "hash",
        "otp",
        "mfa_code",
    }
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _imported_names(tree: ast.Module) -> set[tuple[str, str]]:
    """Every `(module, name)` this file imports, from the parse tree."""
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                found.add((node.module, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name, ""))
    return found


def _calls(tree: ast.Module) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


# --------------------------------------------------------------------------
# 1. The stub never reaches this service
# --------------------------------------------------------------------------


def test_the_boundary_scan_found_the_files_it_is_meant_to_scan():
    """A scan over an empty list passes vacuously. This is the guard on that."""
    names = {path.name for path in SOURCES}

    assert len(SOURCES) >= 11
    assert {"accounts.py", "sessions.py", "seam.py", "app.py", "passwords.py"} <= names


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_file_here_imports_module_19s_identity_stub(path: Path):
    """The seam runs one way: 19/20/21 delegate here, and this delegates nothing back.

    `seam.py` is the one file that may be *imported by* Module 19 — that
    is the whole design — but nothing in this package may import Module
    19's stub-trusting `current_user_id` back, which would make a circle
    in which the bypass became reachable from the service that issues
    credentials.
    """
    for module, name in _imported_names(_tree(path)):
        if module.startswith("services.terminal.identity"):
            assert name not in STUB_NAMES or path.name == "seam.py", (
                f"{path.name} imports {name} from Module 19's identity stub"
            )
        assert not (module == "services.terminal.identity" and name == "current_user_id"), (
            f"{path.name} imports the stub-trusting current_user_id"
        )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_file_here_reads_the_stub_header(path: Path):
    """`X-Argus-User` must be unreachable from this service's routes."""
    source = _tree(path)
    for node in ast.walk(source):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.lower() != "x-argus-user", f"{path.name} names the stub header"


def test_the_identity_app_authenticates_only_through_session_verification():
    """`current_session_user` calls `sessions.verify` and nothing else.

    A future edit that reached for `current_user_id` here — perhaps to
    'reuse the seam' — would make every account-management endpoint
    reachable through the header bypass.
    """
    tree = _tree(PACKAGE / "app.py")
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "current_session_user"
    )
    called = {_call_name(node) for node in _calls(function)}

    assert "verify" in called
    assert "current_user_id" not in called
    assert "resolve_identity" not in called


# --------------------------------------------------------------------------
# 2. Credentials are not response fields
# --------------------------------------------------------------------------


def test_no_response_model_carries_credential_material():
    """Checked against `model_fields`, not source text.

    Two documented exceptions, and only two: the session token returned
    once by login, and the TOTP secret returned once at enrolment. Both
    are values that have to reach the person and exist nowhere else
    afterwards. Everything else about a credential is reported as a
    boolean or not at all.
    """
    from pydantic import BaseModel

    import services.identity.schemas as schemas

    # Two credentials, each returned once at the only moment it can
    # reach the person, plus one field that merely names a scheme.
    allowed = {
        ("SessionResponse", "token"),
        ("EnrolmentResponse", "secret"),
        # "Bearer". A constant naming the auth scheme, not a secret — and
        # listed here rather than loosening the pattern, because a
        # looser pattern is how the real one gets through next time.
        ("SessionResponse", "token_type"),
    }
    responses = {
        name: value
        for name, value in vars(schemas).items()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and name.endswith(("Response", "Summary"))
    }
    assert len(responses) >= 4, "the scan found the response models"

    offenders = [
        (name, field)
        for name, model in responses.items()
        for field in model.model_fields
        if any(part in field.lower() for part in CREDENTIAL_PARTS) and (name, field) not in allowed
    ]
    assert offenders == [], f"credential-shaped response fields: {offenders}"


def test_the_session_summary_has_no_way_to_carry_a_token():
    """Listing your sessions cannot hand back a live credential."""
    from services.identity.schemas import SessionSummary

    assert not any(
        part in field.lower()
        for field in SessionSummary.model_fields
        for part in ("token", "hash", "secret")
    )


def test_the_account_response_exposes_mfa_as_a_boolean_and_never_the_secret():
    from services.identity.schemas import AccountResponse

    assert "mfa_enabled" in AccountResponse.model_fields
    assert "mfa_secret" not in AccountResponse.model_fields
    assert "password_hash" not in AccountResponse.model_fields


# --------------------------------------------------------------------------
# 3. Only the login path issues a session
# --------------------------------------------------------------------------


def test_only_the_login_path_creates_a_session():
    """`sessions.create` does not authenticate — it must follow something that does.

    Walked as calls in the tree rather than grepped, so a rename or a
    reformat cannot make this pass by accident.
    """
    callers: list[str] = []
    for path in SOURCES:
        if path.name == "sessions.py":
            continue  # its own definition
        tree = _tree(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for call in _calls(node):
                func = call.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "create"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "sessions"
                ):
                    callers.append(f"{path.name}:{node.name}")

    assert callers == ["accounts.py:log_in"], f"sessions.create is called from {callers}"


def test_the_login_path_verifies_a_password_before_it_creates_a_session():
    """Order as structure: the verification appears before the issuance."""
    tree = _tree(PACKAGE / "accounts.py")
    login = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "log_in"
    )
    calls = [(_call_name(node), node.lineno) for node in _calls(login)]

    verify_line = next(line for name, line in calls if name == "verify_password")
    create_line = next(line for name, line in calls if name == "create")
    lockout_line = next(line for name, line in calls if name == "lockout_state")

    assert lockout_line < verify_line < create_line


# --------------------------------------------------------------------------
# 4. No credential reaches a logger
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_logging_call_takes_a_credential_shaped_argument(path: Path):
    """The classic way a secret reaches a log aggregator.

    Catches `log.info("...", password)`, `logger.warning(token)` and
    f-strings interpolating either. Walks the call's arguments in the
    tree, so a helpfully-named local variable is caught even though the
    string constant beside it looks innocent.
    """
    logging_methods = {"debug", "info", "warning", "error", "exception", "critical", "log"}

    for call in _calls(_tree(path)):
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr in logging_methods):
            continue
        for argument in ast.walk(ast.Module(body=list(call.args), type_ignores=[])):
            name = (
                argument.id
                if isinstance(argument, ast.Name)
                else argument.attr
                if isinstance(argument, ast.Attribute)
                else ""
            )
            assert not any(part in name.lower() for part in CREDENTIAL_PARTS), (
                f"{path.name}:{call.lineno} logs {name!r}"
            )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_function_here_returns_a_bare_password_argument(path: Path):
    """A function taking `password` and returning it is a leak with a nice name."""
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.FunctionDef):
            continue
        parameters = {argument.arg for argument in node.args.args}
        credential_parameters = {
            name for name in parameters if "password" in name or name == "code"
        }
        if not credential_parameters:
            continue
        for statement in ast.walk(node):
            if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name):
                assert statement.value.id not in credential_parameters, (
                    f"{path.name}:{node.name} returns its own {statement.value.id}"
                )
