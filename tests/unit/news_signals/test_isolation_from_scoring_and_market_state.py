"""The mandatory non-interference guarantee, proved on the parse tree.

Module 28's own spec is explicit: this signal is a display-only
annotation, and `core/market_state/`, `core/scoring/`,
`core/candidate_detection/eligibility/` and `core/live_scanner/` must
never import it — not as a filter, a gate, or a weight, not even
indirectly through the `news_volume_signals` table. A security that is
`BREAKOUT_READY` purely from price/volume structure must be evaluated
identically whether or not this package exists.

Modeled directly on `tests/unit/intelligence/test_boundaries.py`'s "no
computation" AST scan: an import scan proves the wiring is absent
structurally, not just absent from the code paths anyone has read so far —
and it is the one thing a docstring or a code review comment cannot
guarantee on its own.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import core.candidate_detection.eligibility as eligibility_package
import core.live_scanner as live_scanner_package
import core.market_state as market_state_package
import core.news_signals as news_signals_package
import core.scoring as scoring_package

#: The four packages the spec names by name. Each must never import
#: `core.news_signals` or reach into its table.
FORBIDDEN_PACKAGES = {
    "core.market_state": market_state_package,
    "core.scoring": scoring_package,
    "core.candidate_detection.eligibility": eligibility_package,
    "core.live_scanner": live_scanner_package,
}


def _source_files(package) -> list[Path]:
    root = Path(package.__file__).parent
    return sorted(root.rglob("*.py"))


def _all_forbidden_source_files() -> list[tuple[str, Path]]:
    return [
        (package_name, path)
        for package_name, package in FORBIDDEN_PACKAGES.items()
        for path in _source_files(package)
    ]


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


def test_each_forbidden_package_has_source_to_scan():
    """A scan over an empty list passes vacuously — reporting a guarantee
    it never checked."""
    for package_name, package in FORBIDDEN_PACKAGES.items():
        assert len(_source_files(package)) >= 1, package_name


@pytest.mark.parametrize("package_name,path", _all_forbidden_source_files(), ids=lambda v: str(v))
def test_nothing_here_imports_the_news_signal_package(package_name: str, path: Path):
    """The direct guarantee: no module in any of the four packages imports
    `core.news_signals` or a submodule of it."""
    offenders = [
        (line, module, names)
        for line, module, names in _imports(path.read_text())
        if module == "core.news_signals" or module.startswith("core.news_signals.")
    ]
    assert not offenders, (
        f"{package_name} ({path.name}) imports core.news_signals: {offenders}. This "
        "signal must be a display-only annotation with zero influence on watchlist "
        "membership or scoring — see core/news_signals/__init__.py."
    )


@pytest.mark.parametrize("package_name,path", _all_forbidden_source_files(), ids=lambda v: str(v))
def test_nothing_here_imports_the_news_signal_table_directly(package_name: str, path: Path):
    """The indirect route an import-name scan alone would miss: reaching
    the `news_volume_signals` table straight from `infra.db.schema` without
    ever naming `core.news_signals`."""
    offenders = [
        (line, module, names)
        for line, module, names in _imports(path.read_text())
        if module == "infra.db.schema.news_signals"
        or (module == "infra.db.schema" and "news_signals" in names)
        or "news_volume_signals" in names
    ]
    assert not offenders, (
        f"{package_name} ({path.name}) reaches the news_volume_signals table: "
        f"{offenders}. See core/news_signals/__init__.py on why this must stay purely "
        "additive."
    )


@pytest.mark.parametrize("package_name,path", _all_forbidden_source_files(), ids=lambda v: str(v))
def test_the_table_name_appears_nowhere_as_a_string_either(package_name: str, path: Path):
    """Named as a string rather than imported — the shortcut an import scan
    would miss entirely, e.g. a raw SQL query built with `text()`. Checked
    on the parse tree's string constants, so a mention in a docstring is
    the only false-positive risk, and none of these four packages has
    reason to mention this table name in prose either."""
    tree = ast.parse(path.read_text())
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "news_volume_signals" not in strings, (
        f"{package_name} ({path.name}) mentions the news_volume_signals table name."
    )


def test_the_news_signal_package_never_reaches_scoring_market_state_or_eligibility():
    """The converse direction, partly closed: Module 28 does not reach
    *into* scoring, market state or eligibility either — not even to
    read. Its own orchestrator deliberately uses
    `core.data_validation.universe` for membership rather than
    `core.market_state.watchlist`, precisely so this holds.

    `core.live_scanner` is deliberately not included here: the
    orchestrator reuses `core.live_scanner.schedule.scan_date_for`/
    `as_of_for` — the same "which trading day is this" question Modules
    26 and 27 already ask that one function rather than re-deriving —
    which is a one-way, read-only dependency on a scheduling utility, not
    a path back into live-scanner's own decisions. The guarantee this
    project actually needs is the one-directional one tested above: none
    of the four forbidden packages may import `core.news_signals`.
    """
    forbidden_roots = {"core.market_state", "core.scoring", "core.candidate_detection"}
    for path in _source_files(news_signals_package):
        offenders = [
            (line, module)
            for line, module, _names in _imports(path.read_text())
            if any(module == root or module.startswith(root + ".") for root in forbidden_roots)
        ]
        assert not offenders, (
            f"core/news_signals/{path.name} imports a forbidden package: {offenders}"
        )
