"""The AST scan for credential-shaped logging calls, made project-wide.

## What existed before this file

Module 22 built one AST scan, scoped to `services/identity/`: no logging
call there may take an argument named like a password, a token, or a
secret. Module 23 built a second one, scoped to `infra/observability/`,
checking the same thing over its own five files. Both walk the same shape
of tree looking for the same shape of violation, against the same
`CREDENTIAL_KEY_PARTS` word list Module 23 put in `infra.observability.
logging` — Module 22's own list is a subset of it, checked by a test in
this module's suite so the two cannot quietly diverge.

Module 23's own report named the gap plainly: "the other twenty modules
have no loggers yet, so the gap is currently theoretical... now is the
cheap moment to close it." This file is that closing: `scan()` is the one
walk, and it takes the directories to scan rather than assuming which
package it protects — Module 22's and Module 23's checks could both be
rewritten as one call each into this function, and the project-wide test
in `tests/unit/security/` is one call covering everywhere at once.

## What it does not do

It does not touch `services/identity/audit.py`'s scrubber — that
function redacts and refuses at *runtime*, against whatever payload a
caller actually built, and stays exactly as Module 22 left it. This is a
*static* check over source code, run once by a test rather than on every
request, looking for a call site that was never going to be safe no
matter what ran through it. The two are complementary, not duplicates:
this catches the mistake before it ships; the runtime scrubber catches
whatever this cannot see (a value built too dynamically for the AST to
recognise, a dict assembled elsewhere and passed in by reference).

## Where "credential-shaped" comes from

Reused, not reinvented — `infra.observability.logging.
CREDENTIAL_KEY_PARTS`, the same list `fields()` scrubs at runtime. One
word list, so a name added there because it turned out to leak is
immediately what this static scan looks for too.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from infra.observability.logging import CREDENTIAL_KEY_PARTS

__all__ = ["DEFAULT_ROOTS", "LOGGING_METHODS", "Violation", "scan", "scan_file"]

#: The method names `logging.Logger` exposes that can carry a message and
#: fields. Matches Module 22's and Module 23's own scans.
LOGGING_METHODS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical", "log"}
)

#: Every package with application code, scanned by the project-wide test.
#: `tests/`, `.venv/` and anything under a `__pycache__` are never walked.
DEFAULT_ROOTS: tuple[str, ...] = ("core", "services", "data", "infra", "packages")


@dataclass(frozen=True, slots=True)
class Violation:
    """One logging call site that could emit a credential-shaped field."""

    path: Path
    line: int
    argument: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} logs {self.argument!r}"


def scan(roots: Iterable[str | Path] = DEFAULT_ROOTS) -> list[Violation]:
    """Every credential-shaped logging call under the given roots."""
    violations: list[Violation] = []
    for root in roots:
        for path in sorted(Path(root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            violations.extend(scan_file(path))
    return violations


def scan_file(path: Path) -> list[Violation]:
    """Every credential-shaped logging call in one file."""
    try:
        source = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in LOGGING_METHODS):
            continue

        for argument in _flatten(node.args, node.keywords):
            name = _name_of(argument)
            if name and any(part in name.lower() for part in CREDENTIAL_KEY_PARTS):
                violations.append(Violation(path=path, line=node.lineno, argument=name))

    return violations


def _flatten(args: list[ast.expr], keywords: list[ast.keyword]) -> list[ast.expr]:
    """Every value a logging call could emit: positional args, kwarg values,
    and — because `extra={"password": ...}` is how a structured field
    actually reaches a record — the keys and values of any dict literal
    passed as `extra=`.
    """
    flattened: list[ast.expr] = list(args)
    for keyword in keywords:
        if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
            for key, value in zip(keyword.value.keys, keyword.value.values, strict=True):
                if key is not None:
                    flattened.append(key)
                flattened.append(value)
        else:
            flattened.append(keyword.value)
    return flattened


def _name_of(node: ast.expr) -> str:
    """The identifier a value is bound to, if it has an obvious one.

    Covers `log.info(password)`, `log.info(extra={"password": x})` (via
    the dict key, a string constant), and `log.info(extra={"x": user.
    password})` (via the attribute name) — the shapes a credential
    actually reaches a call site in. A value with no name at all (a
    literal, a function call) cannot be credential-*shaped* by name and
    is out of this scan's reach by construction — the runtime scrubber is
    what remains for that case.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""
