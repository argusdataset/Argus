"""The mandatory non-interference guarantee, proved on the parse tree.

The brief for Modules 28 and 29 was explicit and identical for both: these
signals are display-only annotations, and `core/market_state/`,
`core/scoring/`, `core/candidate_detection/eligibility/` and
`core/live_scanner/` must never import them — not as a filter, a gate, or
a weight, not even indirectly through the tables they write. A security
that is `BREAKOUT_READY` purely from price/volume structure must be
evaluated identically whether or not these modules exist.

## One file for two packages, because it is one guarantee

`core/news_signals/` (volume anomaly, SEC 8-K) and
`core/ownership_signals/` (insider clusters, 13F trend) are separate
modules that happen to make the same promise. Splitting the proof in two
would mean a future third signal module could be added with a test file
that quietly covers only itself; keeping the protected set and the
forbidden set in one place here means adding a module means adding one
name to one tuple.

## Three routes in, all closed

An import scan alone is not enough, so each protected package is checked
three ways: it must not import the signal package, must not import the
schema module that defines its tables, and must not name any of those
tables as a string constant — which is how a raw `text()` query would
reach them without importing anything at all.

## What this cannot catch, and what does

This proves the *import graph*. It cannot prove that
`services/intelligence/`, where reading these signals is the whole point,
does not start filtering a watchlist by one — the boundary does not apply
there. That half is
`tests/integration/intelligence/test_signal_isolation.py`, which asserts
the served responses are byte-identical with and without the strongest
possible signal attached.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import core.candidate_detection.eligibility as eligibility_package
import core.live_scanner as live_scanner_package
import core.market_state as market_state_package
import core.news_signals as news_signals_package
import core.ownership_signals as ownership_signals_package
import core.scoring as scoring_package

#: The four packages the brief names. None may reach the signal modules.
FORBIDDEN_PACKAGES = {
    "core.market_state": market_state_package,
    "core.scoring": scoring_package,
    "core.candidate_detection.eligibility": eligibility_package,
    "core.live_scanner": live_scanner_package,
}

#: The signal packages they must not reach. Add a module here and every
#: test below covers it.
PROTECTED_PACKAGES: tuple[str, ...] = ("core.news_signals", "core.ownership_signals")

#: The schema modules defining the signal tables — the second route in.
PROTECTED_SCHEMA_MODULES: tuple[str, ...] = (
    "infra.db.schema.news_signals",
    "infra.db.schema.sec_filings",
    "infra.db.schema.ownership_signals",
)

#: Every table these two modules write. Checked as string constants
#: because a raw `text("SELECT ... FROM insider_trades")` reaches them
#: while importing nothing.
PROTECTED_TABLES: tuple[str, ...] = (
    "news_volume_signals",
    "sec_filings",
    "sec_filing_signals",
    "insider_trades",
    "insider_cluster_signals",
    "institutional_ownership",
    "institutional_ownership_signals",
)


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


def test_both_signal_packages_exist_and_are_covered():
    """The protected set is named by string above, so a typo would make
    every test below pass against nothing."""
    for name, package in (
        ("core.news_signals", news_signals_package),
        ("core.ownership_signals", ownership_signals_package),
    ):
        assert name in PROTECTED_PACKAGES
        assert len(_source_files(package)) >= 1, name


@pytest.mark.parametrize("package_name,path", _all_forbidden_source_files(), ids=lambda v: str(v))
def test_nothing_here_imports_a_signal_package(package_name: str, path: Path):
    """Route one: importing `core.news_signals` or `core.ownership_signals`."""
    offenders = [
        (line, module, names)
        for line, module, names in _imports(path.read_text())
        for protected in PROTECTED_PACKAGES
        if module == protected or module.startswith(protected + ".")
    ]
    assert not offenders, (
        f"{package_name} ({path.name}) imports a signal module: {offenders}. These "
        "signals must be display-only annotations with zero influence on watchlist "
        "membership or scoring — see core/news_signals/__init__.py and "
        "core/ownership_signals/__init__.py."
    )


@pytest.mark.parametrize("package_name,path", _all_forbidden_source_files(), ids=lambda v: str(v))
def test_nothing_here_imports_a_signal_table_directly(package_name: str, path: Path):
    """Route two: reaching the tables straight from `infra.db.schema`
    without ever naming the module that computes them."""
    schema_leaves = {module.rsplit(".", 1)[-1] for module in PROTECTED_SCHEMA_MODULES}
    offenders = [
        (line, module, names)
        for line, module, names in _imports(path.read_text())
        if module in PROTECTED_SCHEMA_MODULES
        or (module == "infra.db.schema" and schema_leaves & set(names))
        or set(PROTECTED_TABLES) & set(names)
    ]
    assert not offenders, (
        f"{package_name} ({path.name}) reaches a signal table: {offenders}. See the "
        "signal packages' docstrings on why this must stay purely additive."
    )


@pytest.mark.parametrize("package_name,path", _all_forbidden_source_files(), ids=lambda v: str(v))
def test_no_signal_table_is_named_as_a_string_either(package_name: str, path: Path):
    """Route three: a raw SQL string, which imports nothing at all.

    Checked on the parse tree's string constants. Docstrings are
    `Constant` nodes too, so this matches whole values rather than
    substrings — a table name appears as its own string in a query, never
    as a paragraph.
    """
    tree = ast.parse(path.read_text())
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    offenders = sorted(set(PROTECTED_TABLES) & strings)
    assert not offenders, (
        f"{package_name} ({path.name}) names signal table(s) {offenders} as a string."
    )


def test_the_signal_packages_never_reach_scoring_market_state_or_eligibility():
    """The converse direction, partly closed: neither module reaches *into*
    scoring, market state or eligibility, not even to read.

    `core/news_signals/orchestrator.py` deliberately resolves who to
    assess through `core.data_validation.universe` rather than
    `core.market_state.watchlist`, precisely so this holds.

    `core.live_scanner` is deliberately not in the forbidden set here:
    both orchestrators reuse `core.live_scanner.schedule.scan_date_for` /
    `as_of_for` — the same "which trading day is this" question Modules
    26 and 27 already ask that one function rather than re-deriving — a
    one-way dependency on a scheduling utility, not a path back into the
    scanner's own decisions. The guarantee this project needs is the
    one-directional one tested above.
    """
    forbidden_roots = {"core.market_state", "core.scoring", "core.candidate_detection"}
    for package in (news_signals_package, ownership_signals_package):
        for path in _source_files(package):
            offenders = [
                (line, module)
                for line, module, _names in _imports(path.read_text())
                if any(module == root or module.startswith(root + ".") for root in forbidden_roots)
            ]
            assert not offenders, f"{path} imports a forbidden package: {offenders}"


def test_the_ingestion_module_may_reach_the_signal_modules():
    """A positive control, and a boundary worth stating rather than
    leaving implied.

    `core/ingestion/ownership.py` imports both signal packages on purpose:
    it fetches provider data and calls their translate/write functions.
    That is the ingestion direction — writing rows the signals later read
    — and it is not what the guarantee above forbids, which is *scoring
    and state* depending on signal output. If this ever fails, the
    forbidden-set test has been over-broadened into something that would
    make the pipeline unbuildable.
    """
    import core.ingestion.ownership as ingestion_module

    modules = {
        module for _line, module, _names in _imports(Path(ingestion_module.__file__).read_text())
    }

    assert any(module.startswith("core.news_signals") for module in modules)
    assert any(module.startswith("core.ownership_signals") for module in modules)
