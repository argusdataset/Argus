"""The four boundaries this module must hold, tested on the parse tree.

Module 20's report flagged a real near-miss: a structural test asserting a
guard clause appeared *before* a loader call in the source text was
defeated by `if False and scope.is_empty` — the text stayed, the behaviour
changed. Every test here inspects actual code structure rather than where
a line sits, which is the lesson applied.

The four boundaries:

1. **No computation.** This module reads and assembles. It must not
   import a scorer, a classifier, a similarity engine or a risk assessor.
2. **No public-stats contact.** Aggregate track-record claims are gated;
   per-security current evidence is not. The two must not meet.
3. **No reused User Watchlist shapes.** A person's list and a derived view
   of `market_state` are different objects.
4. **No fundamentals.** A watchlist entry shows what ARGUS thinks, never
   revenue or P/E — that is the Terminal's question.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import services.intelligence as package
from services.intelligence import blocks, schemas, watchlists

PACKAGE_ROOT = Path(package.__file__).parent
SOURCE_FILES = sorted(PACKAGE_ROOT.glob("*.py"))

#: Functions that *produce* a score, state, similarity or risk. This
#: module may read what they wrote; it may not call them.
COMPUTATION_NAMES: frozenset[str] = frozenset(
    {
        "score_candidate",
        "score_candidates",
        "compute_components",
        "classify_states",
        "detect_candidates",
        "evaluate_eligibility",
        "find_similar_setups",
        "assess_risk_context",
        "assess_invalidation",
        "compute_features",
        "compute_features_batch",
        "record_transitions",
        "advance_lifecycle",
        "assess_news_volume",
        "assess_batch",
        "run_daily_news_signals",
    }
)

#: Fundamentals vocabulary. A field or import named from this set in a
#: score-bearing response would be the Terminal's data on an Intelligence
#: card.
FUNDAMENTALS_TERMS: frozenset[str] = frozenset(
    {
        "revenue",
        "net_income",
        "earnings",
        "eps",
        "pe_ratio",
        "price_earnings_ratio",
        "market_cap",
        "book_value",
        "ebitda",
        "free_cash_flow",
        "fundamentals",
        "statement_type",
        "fiscal_period",
        "valuation",
    }
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
    """A scan over an empty list passes vacuously — reporting a guarantee
    it never checked."""
    assert len(SOURCE_FILES) >= 8


# --------------------------------------------------------------------------
# 1. No computation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_nothing_here_computes_a_score_state_similarity_or_risk(path: Path):
    """The module's central constraint. Every figure it serves was written
    by an earlier module; calling a producer here would create a number
    with no stored provenance."""
    offenders = [
        (line, module, names)
        for line, module, names in _imports(path.read_text())
        if COMPUTATION_NAMES & set(names)
    ]
    assert not offenders, (
        f"{path.name} imports a producer: {offenders}. This module reads and assembles; "
        "computing here would produce a figure no other part of ARGUS could reproduce."
    )


def test_the_only_thing_produced_here_is_module_16s_narration():
    """The one exception, and it is Module 16 producing it, not this module.

    `explain_signal` and `explain_case` are narrators with a fabrication
    guard around their output. Asserted explicitly so the exception is
    visible rather than being an omission from the list above.
    """
    narrators = {
        path.name
        for path in SOURCE_FILES
        for _line, module, _names in _imports(path.read_text())
        if module.startswith("core.explanation")
    }

    assert narrators == {"cases.py", "detail.py"}


# --------------------------------------------------------------------------
# 2. No public-stats contact
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_nothing_here_imports_from_public_stats(path: Path):
    """The mirror image of Module 20's boundary.

    Module 20 proved an unapproved result cannot reach the public page.
    This proves the public page's machinery cannot reach into a live
    per-security view — the gate exists for aggregate track-record claims,
    and joining the two would either gate what should not be gated or
    ungate what should.
    """
    offenders = [
        (line, module)
        for line, module, _names in _imports(path.read_text())
        if module.startswith("services.public_stats")
    ]
    assert not offenders, (
        f"{path.name} imports from services/public_stats: {offenders}. Aggregate "
        "track-record claims are gated; per-security current evidence is not, and the "
        "two must not meet."
    )


def test_nothing_here_reads_the_public_release_gate_tables():
    """Named as strings rather than imported — the shortcut an import scan
    would miss. Checked on the parse tree's string constants, so a mention
    in a docstring does not count."""
    for path in SOURCE_FILES:
        tree = ast.parse(path.read_text())
        strings = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        # Docstrings are Constant nodes too, so match whole values rather
        # than substrings: a table name appears as its own string in a
        # query, never as a paragraph.
        assert "public_release_windows" not in strings
        assert "public_stat_snapshots" not in strings


# --------------------------------------------------------------------------
# 3. No reused User Watchlist shapes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_module_19s_watchlist_schemas_are_never_imported(path: Path):
    """Module 19 flagged this for this module specifically.

    A User Watchlist is a person's list; an Intelligence watchlist is a
    live query over `market_state`. One shape for both would mean a client
    that could not tell a curated list from a derived one.
    """
    forbidden = {"WatchlistItem", "WatchlistDetail", "WatchlistSummary"}
    offenders = [
        (line, module, names)
        for line, module, names in _imports(path.read_text())
        if module.startswith("services.terminal") and forbidden & set(names)
    ]
    assert not offenders, f"{path.name} reuses Module 19's watchlist shapes: {offenders}"


def test_the_intelligence_watchlist_models_are_defined_here():
    """Defined, not aliased. An alias would satisfy the import scan while
    producing the same class."""
    tree = ast.parse(Path(schemas.__file__).read_text())
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}

    assert {"IntelligenceEntry", "IntelligenceWatchlist"} <= defined
    assert schemas.IntelligenceEntry.__module__ == "services.intelligence.schemas"
    assert schemas.IntelligenceWatchlist.__module__ == "services.intelligence.schemas"


def test_the_two_watchlist_shapes_have_genuinely_different_fields():
    """The point of not sharing them, asserted on the models themselves.

    If the fields ever converge, the argument for two shapes has gone and
    somebody should say so deliberately rather than discovering it.
    """
    from services.terminal.schemas import WatchlistItem

    theirs = set(WatchlistItem.model_fields)
    ours = set(schemas.IntelligenceEntry.model_fields)

    # A derived entry carries a state and a score; a user's entry carries
    # where they put it and when they added it.
    assert {"state", "entered_at", "score"} <= ours
    assert {"position", "added_at"} <= theirs
    assert not ({"position", "added_at"} & ours)


# --------------------------------------------------------------------------
# 4. No fundamentals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_nothing_here_reaches_a_fundamentals_read(path: Path):
    offenders = [
        (line, module)
        for line, module, _names in _imports(path.read_text())
        if module
        in {
            "core.data_validation.fundamentals",
            "services.terminal.company",
            "services.terminal.news",
        }
        or module.endswith("schema.canonical")
    ]
    assert not offenders, (
        f"{path.name} reaches fundamentals: {offenders}. An Intelligence entry shows "
        "ARGUS Score numbers; revenue and P/E are the Terminal's question."
    )


def test_no_response_model_carries_a_fundamentals_field():
    """Checked on the Pydantic models' actual fields, not on the source
    text — a field added by a future edit is caught whatever it is called
    in the file."""
    models = [
        value
        for value in vars(schemas).values()
        if inspect.isclass(value)
        and hasattr(value, "model_fields")
        and value.__module__ == "services.intelligence.schemas"
    ]
    assert models

    for model in models:
        overlap = FUNDAMENTALS_TERMS & set(model.model_fields)
        assert not overlap, (
            f"{model.__name__} carries fundamentals field(s) {overlap}. Score numbers "
            "only — see services/intelligence/schemas.py."
        )


def test_the_watchlist_entry_carries_exactly_the_score_numbers():
    """Positive control. The negative tests above pass against a model
    with no fields at all, which is not the same as one carrying the right
    ones."""
    score_fields = set(schemas.ScoreBlock.model_fields)

    assert {
        "argus_score",
        "confidence",
        "opportunity_score",
        "risk_score",
        "probability",
    } <= score_fields


# --------------------------------------------------------------------------
# No independent state storage
# --------------------------------------------------------------------------


def test_the_watchlists_call_module_10s_query_rather_than_their_own(monkeypatch):
    """Source of truth, asserted by substitution rather than by reading.

    Patching Module 10's `watchlist` and seeing the result change proves
    the call is real. A module that had cached, copied or re-derived
    membership would ignore the patch.
    """
    called: list[str] = []

    def _fake(_connection, name):
        called.append(name)
        return []

    monkeypatch.setattr(watchlists, "watchlist", _fake)
    result = watchlists.read_watchlist(None, "CONSOLIDATION", as_of=_now(), stale_after_seconds=1.0)

    assert called == ["CONSOLIDATION"]
    assert result.entries == []
    assert result.stored is False


def test_no_file_here_writes_to_the_database():
    """Read-only by structure. An INSERT or UPDATE anywhere in this module
    would mean it had started keeping its own copy of something."""
    for path in SOURCE_FILES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            assert node.attr not in {"insert", "update", "delete"}, (
                f"{path.name} calls .{node.attr}() — this module reads and assembles, "
                "and storing anything would create a second source of truth."
            )


def test_the_watchlist_state_mapping_comes_from_module_10():
    """Not restated here. A state added to a list in Module 10 must appear
    in this module's answer with no change."""
    from core.market_state.states import WATCHLISTS

    for name, states in WATCHLISTS.items():
        for state in states:
            assert name in blocks.watchlists_for_state(state.value)


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
