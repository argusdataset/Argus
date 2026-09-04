"""Are this module's thresholds really in one place?

The same test Module 10 established and Module 12 repeated, applied here:
every number this module uses is an invented magnitude. If one is written
inline, no behavioural test fails today, and the recalibration this
project's own tooling expects to be possible becomes an archaeology
exercise.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.news_signals import assessment as assessment_module
from core.news_signals import orchestrator as orchestrator_module
from core.news_signals import queries as queries_module
from core.news_signals import signal as signal_module
from core.news_signals.config import (
    CALIBRATABLE,
    STRUCTURAL,
    NewsSignalConfig,
    NewsSignalThreshold,
    NewsSignalThresholds,
)

#: Identity elements and index arithmetic. Anything else in this module's
#: logic is a threshold in disguise.
STRUCTURAL_LITERALS = frozenset({0, 1, -1})

#: Every module that decides anything or drives the daily run.
#: `batch.py` is deliberately excluded, the same way ingestion's own
#: equivalent test excludes `news_writer.py`: its one literal
#: (`DEFAULT_BATCH_SIZE = 1_000`) is an engineering batching constant, not
#: a calibratable decision, and belongs to no config module.
LOGIC_MODULES = (signal_module, queries_module, assessment_module, orchestrator_module)


def _numeric_literals(source: str) -> list[tuple[int, float]]:
    tree = ast.parse(source)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ]


@pytest.mark.parametrize("module", LOGIC_MODULES, ids=lambda m: m.__name__)
def test_no_numeric_literals_in_news_signal_logic(module):
    """Fails at the moment a constant is written, not years later."""
    source = Path(inspect.getfile(module)).read_text()
    offenders = [
        (line, value)
        for line, value in _numeric_literals(source)
        if value not in STRUCTURAL_LITERALS
    ]
    assert not offenders, (
        f"{module.__name__} contains calibration constants inline: {offenders}. "
        "Every threshold belongs in core/news_signals/config.py."
    )


def test_the_whole_threshold_set_is_enumerable_from_one_object():
    thresholds = NewsSignalThresholds()
    values = thresholds.as_dict()

    assert values
    assert set(values) == set(NewsSignalThresholds.names())
    assert all(isinstance(value, float) for value in values.values())


def test_every_threshold_declares_whether_it_is_a_guess():
    for name, entry in NewsSignalThresholds().describe().items():
        assert entry["kind"] in {STRUCTURAL, CALIBRATABLE}, name
        assert entry["rationale"].strip(), f"{name} has no stated reasoning"


def test_every_threshold_here_is_admitted_to_be_a_guess():
    """An honesty check, the mirror of Module 12's own equivalent. Both of
    this module's numbers — the baseline window and the anomaly multiple —
    are invented magnitudes with no validated calibration."""
    thresholds = NewsSignalThresholds()
    calibratable = set(thresholds.calibratable())

    assert calibratable == set(NewsSignalThresholds.names())


def test_the_published_definition_says_the_numbers_are_unvalidated():
    definition = NewsSignalConfig().definition()

    assert set(definition["thresholds"]) == set(NewsSignalThresholds.names())
    assert definition["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"


def test_a_stored_definition_round_trips_back_into_thresholds():
    """A stored signal must be re-derivable from the version it cites."""
    original = NewsSignalThresholds()
    rebuilt = NewsSignalThresholds.from_definition(
        NewsSignalConfig(thresholds=original).definition()
    )

    assert rebuilt.as_dict() == original.as_dict()
    for name in NewsSignalThresholds.names():
        assert getattr(rebuilt, name).rationale == getattr(original, name).rationale


def test_changing_one_threshold_changes_the_configuration_checksum():
    """A recalibration is forced to become a new version label."""
    baseline = NewsSignalConfig()
    changed = NewsSignalConfig(
        thresholds=NewsSignalThresholds(
            anomaly_multiple=NewsSignalThreshold(
                value=5.0, kind=CALIBRATABLE, rationale="recalibrated"
            )
        )
    )
    assert baseline.content_checksum() != changed.content_checksum()
    assert baseline.version_label() != changed.version_label()


def test_the_baseline_window_is_a_threshold_not_a_hardcoded_timedelta():
    """The window governs the aggregate query's own date arithmetic, so it
    must be recalibratable too — not buried as `timedelta(days=30)` at a
    call site where an auditor would never find it."""
    from datetime import timedelta

    thresholds = NewsSignalThresholds(
        baseline_window_days=NewsSignalThreshold(value=45.0, kind=CALIBRATABLE, rationale="test")
    )
    assert thresholds.window == timedelta(days=45)
    assert thresholds.window_days == 45
