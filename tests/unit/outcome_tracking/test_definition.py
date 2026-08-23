"""The success definition, and keeping its numbers in one place.

The criterion this module applies is the dataset's ground truth: every
statistic Module 17 computes, and the eventual answer to whether the
pattern works, is measured against labels assigned by these three numbers.
A criterion written inline would be the single most consequential hidden
constant in the project.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.outcome_tracking import case_record as case_module
from core.outcome_tracking import classification as classification_module
from core.outcome_tracking import engine as engine_module
from core.outcome_tracking import excursion as excursion_module
from core.outcome_tracking.config import (
    CALIBRATABLE,
    STRUCTURAL,
    SUCCESS_DEFINITION,
    OutcomeConfig,
    OutcomeThreshold,
    OutcomeThresholds,
)
from core.scoring.config import PROBABILITY_DEFINITION

STRUCTURAL_LITERALS = frozenset({0, 1, -1, 2})

LOGIC_MODULES = (
    excursion_module,
    classification_module,
    case_module,
    engine_module,
)


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
def test_no_numeric_literals_in_outcome_logic(module):
    source = Path(inspect.getfile(module)).read_text()
    offenders = [
        (line, value)
        for line, value in _numeric_literals(source)
        if value not in STRUCTURAL_LITERALS
    ]
    assert not offenders, (
        f"{module.__name__} contains calibration constants inline: {offenders}. "
        "The success criterion belongs in core/outcome_tracking/config.py."
    )


def test_the_success_definition_matches_module_13s_probability_definition():
    """Load-bearing, not tidy.

    Module 13 reserves `probability` for a calibrated model that does not
    exist yet. When it is built it will be fitted against the labels this
    module assigns. If the two definitions drift, the model is calibrated
    to predict something other than what the dataset records — and nothing
    fails, the numbers simply mean something nobody intended.
    """
    assert SUCCESS_DEFINITION == PROBABILITY_DEFINITION


def test_the_definition_and_the_thresholds_say_the_same_thing():
    """A definition string that drifted from the numbers applying it would
    be worse than no string at all."""
    thresholds = OutcomeThresholds()

    assert f"+{thresholds.target_gain.value:.0%}" in SUCCESS_DEFINITION
    assert f"-{abs(thresholds.stop_loss.value):.0%}" in SUCCESS_DEFINITION
    assert f"{thresholds.horizon_trading_days.value:.0f} trading days" in SUCCESS_DEFINITION


def test_the_stop_is_negative_and_the_target_positive():
    """Sign conventions, asserted. A stop stored as a positive magnitude
    would silently invert every failure test."""
    thresholds = OutcomeThresholds()

    assert thresholds.target_gain.value > 0
    assert thresholds.stop_loss.value < 0
    assert thresholds.breakdown_floor.value < thresholds.stop_loss.value


def test_every_number_is_enumerable_and_declares_its_kind():
    described = OutcomeThresholds().describe()

    assert set(described) == set(OutcomeThresholds.names())
    for name, entry in described.items():
        assert entry["kind"] in {STRUCTURAL, CALIBRATABLE}, name
        assert entry["rationale"].strip(), f"{name} has no stated reasoning"


def test_most_of_the_criterion_is_admitted_to_be_invented():
    thresholds = OutcomeThresholds()
    calibratable = set(thresholds.calibratable())

    assert len(calibratable) > len(set(OutcomeThresholds.names()) - calibratable)
    assert {"target_gain", "stop_loss", "horizon_trading_days"} <= calibratable


def test_the_published_definition_says_the_numbers_are_unvalidated():
    definition = OutcomeConfig().definition()

    assert definition["success_definition"] == SUCCESS_DEFINITION
    assert definition["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"
    assert set(definition["thresholds"]) == set(OutcomeThresholds.names())


def test_a_stored_definition_round_trips_back_into_thresholds():
    original = OutcomeThresholds()
    rebuilt = OutcomeThresholds.from_definition(OutcomeConfig(thresholds=original).definition())

    assert rebuilt.as_dict() == original.as_dict()
    for name in OutcomeThresholds.names():
        assert getattr(rebuilt, name).rationale == getattr(original, name).rationale


def test_changing_the_criterion_changes_the_configuration_checksum():
    """A changed criterion relabels the dataset, so it has to become a new
    snapshot and outcomes computed under the old one stay attributable."""
    baseline = OutcomeConfig()
    changed = OutcomeConfig(
        thresholds=OutcomeThresholds(
            target_gain=OutcomeThreshold(value=0.20, kind=CALIBRATABLE, rationale="recalibrated")
        )
    )

    assert baseline.content_checksum() != changed.content_checksum()
    assert baseline.version_label() != changed.version_label()
