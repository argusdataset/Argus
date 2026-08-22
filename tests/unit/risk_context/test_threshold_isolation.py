"""Are this module's thresholds really in one place?

The same test Module 10 established, applied here for the same reason and
with more force: every number in this module is an invented magnitude. If
one of them is written inline, no behavioural test fails, the module works
fine, and the recalibration that Module 17 makes possible becomes an
archaeology exercise.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.risk_context import assessment as assessment_module
from core.risk_context import events as events_module
from core.risk_context import flags as flags_module
from core.risk_context import invalidation as invalidation_module
from core.risk_context.config import (
    CALIBRATABLE,
    STRUCTURAL,
    RiskConfig,
    RiskThreshold,
    RiskThresholds,
)

#: Identity elements, index arithmetic, and the bounds of a normalized
#: scale. Anything else in this module's logic is a threshold in disguise.
STRUCTURAL_LITERALS = frozenset({0, 1, -1, 2})

#: Every module that decides anything.
LOGIC_MODULES = (events_module, flags_module, invalidation_module, assessment_module)


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
def test_no_numeric_literals_in_risk_logic(module):
    """Fails at the moment a constant is written, not years later."""
    source = Path(inspect.getfile(module)).read_text()
    offenders = [
        (line, value)
        for line, value in _numeric_literals(source)
        if value not in STRUCTURAL_LITERALS
    ]
    assert not offenders, (
        f"{module.__name__} contains calibration constants inline: {offenders}. "
        "Every threshold belongs in core/risk_context/config.py."
    )


def test_the_whole_threshold_set_is_enumerable_from_one_object():
    thresholds = RiskThresholds()
    values = thresholds.as_dict()

    assert values
    assert set(values) == set(RiskThresholds.names())
    assert all(isinstance(value, float) for value in values.values())


def test_every_threshold_declares_whether_it_is_a_guess():
    for name, entry in RiskThresholds().describe().items():
        assert entry["kind"] in {STRUCTURAL, CALIBRATABLE}, name
        assert entry["rationale"].strip(), f"{name} has no stated reasoning"


def test_almost_every_threshold_here_is_admitted_to_be_a_guess():
    """An honesty check. This module invented nearly all of its numbers.

    If a future edit quietly reclassified magnitudes as structural, the
    set would read as far better grounded than it is — the single most
    misleading change anyone could make to this file.
    """
    thresholds = RiskThresholds()
    calibratable = set(thresholds.calibratable())
    structural = set(RiskThresholds.names()) - calibratable

    assert len(calibratable) > len(structural)


def test_the_published_definition_says_the_numbers_are_unvalidated():
    definition = RiskConfig().definition()

    assert set(definition["thresholds"]) == set(RiskThresholds.names())
    assert definition["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"


def test_a_stored_definition_round_trips_back_into_thresholds():
    """A stored risk assessment must be re-derivable from the version it cites."""
    original = RiskThresholds()
    rebuilt = RiskThresholds.from_definition(RiskConfig(thresholds=original).definition())

    assert rebuilt.as_dict() == original.as_dict()
    for name in RiskThresholds.names():
        assert getattr(rebuilt, name).rationale == getattr(original, name).rationale


def test_changing_one_threshold_changes_the_configuration_checksum():
    """A recalibration is forced to become a new version label."""
    baseline = RiskConfig()
    changed = RiskConfig(
        thresholds=RiskThresholds(
            imminent_event_days=RiskThreshold(
                value=21.0, kind=CALIBRATABLE, rationale="recalibrated"
            )
        )
    )
    assert baseline.content_checksum() != changed.content_checksum()
    assert baseline.version_label() != changed.version_label()


def test_the_ingestion_lag_is_a_threshold_not_a_hardcoded_timedelta():
    """The lag governs PIT availability, so it must be recalibratable too.

    Buried in `PitTimestamps.derive(lag=timedelta(hours=12))` at the call
    site it would be invisible to anyone auditing what ARGUS assumes about
    how quickly it can act on a calendar.
    """
    from datetime import timedelta

    thresholds = RiskThresholds(
        ingestion_lag=RiskThreshold(value=48.0, kind=CALIBRATABLE, rationale="test")
    )
    assert thresholds.ingestion_lag_delta == timedelta(hours=48)
