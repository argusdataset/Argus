"""The module cannot read outside its input surface — enforced, not promised.

The Module 16 brief requires the input surface be "explicitly, narrowly
defined and enforced (a test confirms the module cannot read outside it)".
A test that merely checked *what this module currently reads* would pass
forever while someone added a query; these check what it is *able* to
read, which is a property of the imports.

The result is stronger than the brief asked for: `core/explanation`
imports nothing from `core.*` at all beyond itself. Every input arrives as
a plain dictionary — the `as_dict()` output of an upstream module — so
there is no object graph to walk back to a connection and no table to
reach for.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import core.explanation
from core.explanation import INPUT_SURFACE

PACKAGE = Path(core.explanation.__file__).parent
SOURCES = sorted(PACKAGE.glob("*.py"))

#: Anything that would let this module reach data on its own.
FORBIDDEN_ROOTS: frozenset[str] = frozenset(
    {"sqlalchemy", "psycopg", "alembic", "infra", "data", "services", "httpx", "requests"}
)


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _top_level_modules(path: Path) -> set[str]:
    """Only imports at module scope.

    Separate from `_imported_modules`, which walks the whole tree on
    purpose: a lazily-imported `sqlalchemy` would be exactly as much of a
    boundary violation as a top-level one, so the forbidden-root scan must
    see nested imports. Whether a dependency is *optional*, though, is
    precisely a question about module scope.
    """
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_there_are_sources_to_scan():
    """A glob that silently matched nothing would make every test below
    vacuously true."""
    assert len(SOURCES) >= 5


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_database_or_network_access_is_even_importable(path):
    """The enforcement. This module cannot open a connection, cannot name
    a table, and cannot make an HTTP call, because none of it is imported.

    `anthropic` is deliberately absent from the forbidden list and equally
    deliberately imported lazily inside a method — the optional renderer
    seam. Everything else that could fetch data is barred outright.
    """
    offenders = _imported_roots(path) & FORBIDDEN_ROOTS

    assert not offenders, (
        f"{path.name} imports {sorted(offenders)}. Module 16 narrates finished "
        "evidence and takes plain dictionaries; reaching for data itself would put "
        "it outside its declared input surface."
    )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_nothing_is_imported_from_another_core_module(path):
    """Stronger than the brief requires, and worth keeping.

    Every input is an `as_dict()` payload, so there is no upstream type to
    import — which means there is no object to walk back to a connection,
    and no way to quietly start reading a sixth source.
    """
    outside = {
        module
        for module in _imported_modules(path)
        if module.startswith("core.") and not module.startswith("core.explanation")
    }

    assert not outside, f"{path.name} imports {sorted(outside)} from outside this package."


def test_no_public_function_accepts_a_connection():
    """Belt and braces against the import scan being satisfied by a
    late-bound argument."""
    import inspect

    for name in core.explanation.__all__:
        member = getattr(core.explanation, name)
        if not callable(member) or inspect.isclass(member):
            continue
        parameters = inspect.signature(member).parameters
        assert "connection" not in parameters, name
        assert "conn" not in parameters, name


def test_the_input_surface_is_declared_and_names_five_sources():
    """The list is a constant so the boundary is greppable, and so this
    test has something to assert against rather than a docstring."""
    assert len(INPUT_SURFACE) == 5
    assert set(INPUT_SURFACE) == {
        "module_13_signal",
        "module_15_case_record",
        "module_12_risk_context",
        "module_11_similarity",
        "module_10_state_evidence",
    }


def test_the_optional_dependency_is_never_imported_at_module_level():
    """`anthropic` is optional, and this is what keeps it optional.

    A top-level `import anthropic` anywhere in the package would make the
    whole of ARGUS depend on it — every downstream test would need a
    network-capable dependency installed to import this module at all. The
    renderer imports it inside a method instead, so the seam exists
    without the dependency being required.
    """
    top_level = {module for path in SOURCES for module in _top_level_modules(path)}

    assert "anthropic" not in top_level
    # And it really is referenced somewhere — otherwise this test would
    # pass on a module that had quietly lost its renderer.
    assert any("anthropic" in path.read_text() for path in SOURCES)
