"""Does the Terminal actually stay out of the scoring pipeline?

The project has been explicit since early planning: fundamentals and news
are a **context layer**, never a `target-model-v1` or `argus_score` input.
Module 13 keeps `fundamental_context` at 0% weight for that reason.

A rule like that survives exactly as long as somebody is watching it. So
it is enforced the way Modules 16 and 18 enforce their own boundaries: an
AST scan over this module's source, failing on the import rather than on
the consequence.

## Direct imports, not transitive ones — and that is the right scope

`services/terminal/freshness.py` imports Module 18's `results.py`, which
imports `ScoringDecision` from `core.scoring.gating` to name the values a
decision column can hold. A transitive scan would flag that and the only
way to satisfy it would be to stop using Module 18's read surface, which
the module brief requires this module to use.

The rule that matters is "the Terminal must not reach into scoring", and
reaching means calling. Importing an enum three modules away to read a
column is not reaching; writing `from core.scoring.engine import
score_candidate` here is. The scan tests the second, which is the thing
that would actually create a path from fundamentals to a score.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import services.terminal as terminal_package

#: Import roots this module may never name directly. Scoring and market
#: state are the two that carry the fundamentals-must-not-reach-scoring
#: rule; the third is the User-vs-Intelligence watchlist boundary.
FORBIDDEN_ROOTS: tuple[str, ...] = (
    "core.scoring",
    "core.market_state",
    "core.candidate_detection",
    "core.historical_similarity",
    "core.feature_engine.groups",
)

TERMINAL_ROOT = Path(terminal_package.__file__).parent
SOURCE_FILES = sorted(TERMINAL_ROOT.glob("*.py"))


def _imported_modules(source: str) -> list[tuple[int, str]]:
    """Every module named by an import, including inside functions."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def test_the_module_has_source_to_scan():
    """A scan over an empty list passes vacuously, which is worse than no
    scan at all — it reports a guarantee it never checked."""
    assert len(SOURCE_FILES) >= 8


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_the_terminal_never_imports_the_scoring_pipeline(path: Path):
    offenders = [
        (line, module)
        for line, module in _imported_modules(path.read_text())
        if any(module == root or module.startswith(f"{root}.") for root in FORBIDDEN_ROOTS)
    ]
    assert not offenders, (
        f"{path.name} imports from the scoring pipeline: {offenders}. Fundamentals and "
        "news are a context layer and must never reach argus_score — see Module 13's "
        "fundamental_context at 0% weight."
    )


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_the_terminal_never_reads_market_state_tables(path: Path):
    """The other half of the boundary, and the one a determined shortcut
    would take: skipping the import and querying `market_state` directly.

    ARGUS Intelligence Watchlists (DOWN TREND / CONSOLIDATION / BREAKOUT
    READY) are derived from that table and belong to Module 21. Not even a
    read-only version of them is built here.
    """
    source = path.read_text()
    for table in ("market_state", "market_state_transitions", "signals"):
        assert f'"{table}"' not in source and f"'{table}'" not in source, (
            f"{path.name} names the {table} table. Intelligence output is Module 21's; "
            "the Terminal serves company data and user watchlists."
        )


def test_the_only_scan_output_the_terminal_reads_is_module_18s_read_surface():
    """One file may touch scan output, and only through `results.py`.

    Module 18's report made its `results.py` the intended read surface.
    A second query anywhere in this module would duplicate what that
    already does correctly — including resolving a calendar date through
    `live_scan_runs` rather than reconstructing a session-close offset.
    """
    touching = {
        path.name
        for path in SOURCE_FILES
        if any(
            module.startswith("core.live_scanner")
            for _line, module in _imported_modules(path.read_text())
        )
    }
    assert touching == {"freshness.py"}


def test_nothing_in_the_terminal_imports_live_scan_internals():
    """`results.py` only. Not `scanner`, not `runs`, not `schedule` —
    those are the scanner's operational machinery, and a Terminal that
    reached into them would be running scans rather than reading them."""
    for path in SOURCE_FILES:
        for line, module in _imported_modules(path.read_text()):
            if module.startswith("core.live_scanner"):
                assert module == "core.live_scanner.results", (
                    f"{path.name}:{line} imports {module}. The Terminal reads scan "
                    "output through results.py and nothing else."
                )
