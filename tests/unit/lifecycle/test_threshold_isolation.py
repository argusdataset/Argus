"""Are the lifecycle's thresholds really in one place?

The same source scan Modules 10, 12 and 13 established. It matters here
for a narrower reason than usual: this module has few numbers, and the two
that decide qualification sit directly between "ARGUS noticed a base" and
"ARGUS is tracking it". A bar written inline would be the least visible
and most consequential constant in the system.
"""

from __future__ import annotations

import ast
import inspect
from datetime import timedelta
from pathlib import Path

import pytest

from core.lifecycle import derivation as derivation_module
from core.lifecycle import engine as engine_module
from core.lifecycle import events as events_module
from core.lifecycle.config import (
    CALIBRATABLE,
    STRUCTURAL,
    LifecycleConfig,
    LifecycleThreshold,
    QualificationThresholds,
)

#: Identity elements and index arithmetic. Anything else in lifecycle
#: logic is a threshold in disguise.
STRUCTURAL_LITERALS = frozenset({0, 1, -1, 2})

LOGIC_MODULES = (events_module, derivation_module, engine_module)


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
def test_no_numeric_literals_in_lifecycle_logic(module):
    source = Path(inspect.getfile(module)).read_text()
    offenders = [
        (line, value)
        for line, value in _numeric_literals(source)
        if value not in STRUCTURAL_LITERALS
    ]
    assert not offenders, (
        f"{module.__name__} contains calibration constants inline: {offenders}. "
        "Every threshold belongs in core/lifecycle/config.py."
    )


def test_the_whole_threshold_set_is_enumerable_from_one_object():
    thresholds = QualificationThresholds()
    values = thresholds.as_dict()

    assert values
    assert set(values) == set(QualificationThresholds.names())
    assert all(isinstance(value, float) for value in values.values())


def test_every_threshold_declares_whether_it_is_a_guess():
    for name, entry in QualificationThresholds().describe().items():
        assert entry["kind"] in {STRUCTURAL, CALIBRATABLE}, name
        assert entry["rationale"].strip(), f"{name} has no stated reasoning"


def test_most_of_these_numbers_are_admitted_to_be_invented():
    thresholds = QualificationThresholds()
    calibratable = set(thresholds.calibratable())
    structural = set(QualificationThresholds.names()) - calibratable

    assert len(calibratable) > len(structural)


def test_the_published_definition_says_the_numbers_are_unvalidated():
    definition = LifecycleConfig().definition()

    assert set(definition["thresholds"]) == set(QualificationThresholds.names())
    assert definition["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"


def test_a_stored_definition_round_trips_back_into_thresholds():
    original = QualificationThresholds()
    rebuilt = QualificationThresholds.from_definition(
        LifecycleConfig(thresholds=original).definition()
    )

    assert rebuilt.as_dict() == original.as_dict()
    for name in QualificationThresholds.names():
        assert getattr(rebuilt, name).rationale == getattr(original, name).rationale


def test_changing_the_qualification_bar_changes_the_configuration_checksum():
    """A recalibration is forced to become a new version label, so a setup
    qualified under an old bar stays attributable to it."""
    baseline = LifecycleConfig()
    changed = LifecycleConfig(
        thresholds=QualificationThresholds(
            min_argus_score=LifecycleThreshold(
                value=70.0, kind=CALIBRATABLE, rationale="recalibrated"
            )
        )
    )

    assert baseline.content_checksum() != changed.content_checksum()
    assert baseline.version_label() != changed.version_label()


def test_the_expiry_windows_are_thresholds_not_hardcoded_timedeltas():
    """Buried in a `timedelta(days=180)` at the call site, the tracking
    window would be invisible to anyone auditing how long ARGUS waits."""
    thresholds = QualificationThresholds(
        max_active_duration_days=LifecycleThreshold(value=30.0, kind=CALIBRATABLE, rationale="test")
    )

    assert thresholds.active_window == timedelta(days=30)
    assert thresholds.detection_window == timedelta(
        days=QualificationThresholds().max_detection_duration_days.value
    )


def test_the_detection_window_is_longer_than_the_active_one():
    """An honesty check on the pair. Detection is where every setup lives
    today, and closing one early costs exactly the slow base ARGUS exists
    to find; ACTIVE is a committed position and should not be held open
    indefinitely. Inverting them would quietly reverse both intentions."""
    thresholds = QualificationThresholds()

    assert thresholds.detection_window > thresholds.active_window
