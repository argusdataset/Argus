"""Can an unapproved result reach the public page? — asserted structurally.

The requirement is absolute: nothing derived from a `PENDING_REVIEW` or
`REJECTED` result may be servable. An integration test can show that the
endpoints as written today respect it. Only a structural test can show
that the *next* endpoint will too.

So the shape is the guarantee, and these tests are what hold the shape:

- `gate.py` is the only file that may load outcomes.
- Nothing else may name the outcome tables or import a loader.
- Nothing may import Module 17's review internals to build its own
  approval query — `approved_runs` is reached through `gate.py` and
  nowhere else.
- The gate's own loader has no argument that widens it.

Matching the AST-scan discipline Modules 16, 18 and 19 established for
their own boundaries.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import services.public_stats as package
from services.public_stats import gate

PACKAGE_ROOT = Path(package.__file__).parent
SOURCE_FILES = sorted(PACKAGE_ROOT.glob("*.py"))
GATE_FILE = Path(gate.__file__).name

#: Loading published outcomes is `gate.py`'s job alone.
LOADER_NAMES: tuple[str, ...] = (
    "load_evaluation_dataset",
    "load_cases",
    "compute_case",
    "process_concluded_setups",
)

#: Tables holding results. Named as strings by a query that bypassed the
#: gate, which is the shortcut a hurried endpoint would take.
RESULT_TABLES: tuple[str, ...] = (
    "setups",
    "setup_outcomes",
    "setup_events",
    "signals",
)

#: Module 17's gate. Reachable from `gate.py` and nowhere else — a second
#: caller is a second place the approval rule can be got wrong.
REVIEW_IMPORTS: tuple[str, ...] = (
    "core.model_validation_evaluation.validation.review",
    "core.model_validation_evaluation.validation.runs",
)


def _imports(source: str) -> list[tuple[int, str, tuple[str, ...]]]:
    """(line, module, imported names) for every import, nested ones included."""
    tree = ast.parse(source)
    found: list[tuple[int, str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name, ()) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module, tuple(alias.name for alias in node.names)))
    return found


def test_the_module_has_source_to_scan():
    """A scan over an empty list passes vacuously, reporting a guarantee
    it never checked."""
    assert len(SOURCE_FILES) >= 7
    assert GATE_FILE in {path.name for path in SOURCE_FILES}


@pytest.mark.parametrize(
    "path", [p for p in SOURCE_FILES if p.name != GATE_FILE], ids=lambda p: p.name
)
def test_only_the_gate_may_load_outcomes(path: Path):
    offenders = [
        (line, module, names)
        for line, module, names in _imports(path.read_text())
        if any(loader in names for loader in LOADER_NAMES)
    ]
    assert not offenders, (
        f"{path.name} imports a dataset loader: {offenders}. Loading published outcomes "
        "is gate.py's job alone — a second loader is a second place the approval "
        "filter can be omitted."
    )


@pytest.mark.parametrize(
    "path", [p for p in SOURCE_FILES if p.name != GATE_FILE], ids=lambda p: p.name
)
def test_only_the_gate_may_reach_module_17s_review_functions(path: Path):
    offenders = [
        (line, module)
        for line, module, _names in _imports(path.read_text())
        if module in REVIEW_IMPORTS
    ]
    assert not offenders, (
        f"{path.name} imports Module 17's review internals: {offenders}. The approval "
        "question is answered in gate.py and nowhere else."
    )


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_no_file_names_a_result_table_directly(path: Path):
    """The shortcut a hurried endpoint takes: skip the loader, write SQL.

    `gate.py` is included in this scan, not exempted — it reaches results
    through Module 17's loader, which already applies the period and
    universe bounds correctly, and a raw query even there would be a
    second implementation of the same filter.
    """
    source = path.read_text()
    for table in RESULT_TABLES:
        for quoted in (f'"{table}"', f"'{table}'", f"FROM {table}", f"from {table}"):
            assert quoted not in source, (
                f"{path.name} names the {table} table as a string ({quoted!r}). Published "
                "results are reached through gate.py's loader, which carries the "
                "approval bounds."
            )


def test_the_published_loader_takes_no_argument_that_could_widen_it():
    """A period parameter would be an override with a friendly name.

    `published_dataset` derives every bound from the scope it is handed,
    and the scope is built only by `current_scope`. If a future signature
    grows a `period_start` or a `universe_version_id`, this fails — which
    is the moment to ask why.
    """
    parameters = set(inspect.signature(gate.published_dataset).parameters)

    assert parameters == {"connection", "scope", "as_of"}


def test_the_scope_builder_takes_nothing_but_a_connection():
    """`current_scope(connection)` and nothing else. A parameter here
    would be the override the requirement forbids."""
    assert set(inspect.signature(gate.current_scope).parameters) == {"connection"}


def test_an_empty_gate_yields_an_empty_scope_not_an_unbounded_one():
    """The specific bug this guards: a filter built from an empty list is
    not a filter. An empty scope must be *recognised* as empty rather
    than falling through to a load with no bounds."""
    empty = gate.PublishScope()

    assert empty.is_empty
    assert empty.runs == ()
    assert empty.windows == ()


def test_every_load_is_driven_by_a_scope_member():
    """The mechanism, not a guard in front of it.

    An earlier version of this test asserted that `scope.is_empty` appears
    before the first loader call in the source. A deliberate break —
    `if False and scope.is_empty` — kept the text and passed it, and the
    break turned out to be a no-op anyway: removing the guard changes
    nothing, because both loads are driven by `for ... in scope.runs` and
    `for ... in scope.windows`, and an empty scope means empty loops.

    So the property worth pinning is the loop structure itself. Every call
    to the loader must sit inside a `for` over a scope collection — a call
    outside one is a load with no bounds, which is the actual bug.
    """
    tree = ast.parse(Path(gate.__file__).read_text())
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "published_dataset"
    )

    def _calls(node: ast.AST) -> list[ast.Call]:
        return [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "load_evaluation_dataset"
        ]

    inside_loops = [
        call for loop in ast.walk(target) if isinstance(loop, ast.For) for call in _calls(loop)
    ]
    everywhere = _calls(target)

    assert everywhere, "published_dataset no longer loads anything at all"
    assert len(inside_loops) == len(everywhere), (
        "published_dataset loads outside a loop over the scope. Every load must be "
        "driven by a scope member — one that is not is a query with no approval bounds."
    )


def test_the_only_functions_returning_publishable_sets_are_the_two_gates():
    """`approved_runs` (Module 17) and `approved_windows` (this module).

    Both are named in `gate.py`; neither takes a parameter that relaxes
    it. This asserts nothing else in the package offers a third door.
    """
    doors = {
        path.name
        for path in SOURCE_FILES
        for _line, _module, names in _imports(path.read_text())
        if "approved_runs" in names
    }

    assert doors == {GATE_FILE}


def test_the_aggregates_cannot_reach_a_database_at_all():
    """Every chart builder is frame-in, payload-out.

    A builder that could open a connection could load whatever it liked,
    and the structural guarantee would become a guarantee about intent.

    Checked on the parse tree rather than by searching the text: the
    module's own docstring explains at length why it holds no connection,
    and a substring scan flagged the explanation. A test that a comment
    can fail is a test that gets its comment edited.
    """
    from services.public_stats import aggregates

    source = Path(aggregates.__file__).read_text()
    tree = ast.parse(source)

    for _line, module, _names in _imports(source):
        assert not module.startswith("sqlalchemy"), (
            "aggregates.py imports SQLAlchemy. Chart builders take an already-loaded "
            "frame; one that could query would sit outside the gate."
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        arguments = node.args
        names = {
            arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
        }
        assert "connection" not in names, (
            f"aggregates.{node.name} takes a connection. Chart builders compute from a "
            "frame the gate already produced."
        )
