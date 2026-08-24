"""Are this module's numbers really in one place, and is `operational` real?

The same AST source scan Modules 10, 12, 13 and 14 established. It matters
here for a reason none of those had: this module *produces the evidence*
that recalibrates all of theirs. A threshold buried inline in an
evaluation would be a number nobody could find while recalibrating
everything else against its output.

This module also introduces a third kind, `operational`, and a kind that
is only ever asserted about is a kind nobody checks. So there is a test
below that takes the claim literally: an operational constant must
provably not change a result.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.model_validation_evaluation.evaluation import (
    breakdowns as breakdowns_module,
)
from core.model_validation_evaluation.evaluation import buckets as buckets_module
from core.model_validation_evaluation.evaluation import dataset as dataset_module
from core.model_validation_evaluation.evaluation import engine as evaluation_engine_module
from core.model_validation_evaluation.evaluation import metrics as metrics_module
from core.model_validation_evaluation.evaluation import walk_forward as walk_forward_module
from core.model_validation_evaluation.evaluation.config import (
    EvaluationConfig,
    EvaluationThreshold,
    EvaluationThresholds,
    ScoreBucketing,
)
from core.model_validation_evaluation.validation import replay as replay_module
from core.model_validation_evaluation.validation import review as review_module
from core.model_validation_evaluation.validation import runs as runs_module
from core.model_validation_evaluation.validation import versions as versions_module
from core.model_validation_evaluation.validation.config import (
    CALIBRATABLE,
    KINDS,
    OPERATIONAL,
    STRUCTURAL,
    ReplaySettings,
    ValidationConfig,
    ValidationSetting,
)

#: Identity elements and index arithmetic. Anything else is a threshold in
#: disguise. Matches Modules 10-14 exactly.
STRUCTURAL_LITERALS = frozenset({0, 1, -1, 2})

LOGIC_MODULES = (
    replay_module,
    review_module,
    runs_module,
    versions_module,
    metrics_module,
    buckets_module,
    dataset_module,
    breakdowns_module,
    walk_forward_module,
    evaluation_engine_module,
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


@pytest.mark.parametrize("module", LOGIC_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_no_numeric_literals_in_module_17_logic(module):
    source = Path(inspect.getfile(module)).read_text()
    offenders = [
        (line, value)
        for line, value in _numeric_literals(source)
        if value not in STRUCTURAL_LITERALS
    ]
    assert not offenders, (
        f"{module.__name__} contains constants inline: {offenders}. Every one belongs "
        "in validation/config.py or evaluation/config.py."
    )


def test_both_threshold_sets_are_enumerable_from_one_object():
    for thresholds in (ReplaySettings(), EvaluationThresholds()):
        values = thresholds.as_dict()
        assert values
        assert set(values) == set(type(thresholds).names())
        assert all(isinstance(value, float) for value in values.values())


def test_every_number_declares_which_of_the_three_kinds_it_is():
    for thresholds in (ReplaySettings(), EvaluationThresholds()):
        for name, entry in thresholds.describe().items():
            assert entry["kind"] in KINDS, name
            assert entry["rationale"].strip(), f"{name} has no stated reasoning"


def test_the_score_buckets_carry_a_kind_and_a_rationale_like_everything_else():
    """They are a tuple rather than a float, which must not exempt them."""
    buckets = ScoreBucketing()

    assert buckets.kind == CALIBRATABLE
    assert "before any score distribution existed" in buckets.rationale
    assert buckets.describe()["score_range"]["kind"] == STRUCTURAL
    assert buckets.as_dict()["edges"] == list(buckets.edges)


def test_most_of_these_numbers_are_admitted_to_be_invented():
    thresholds = EvaluationThresholds()
    calibratable = set(thresholds.calibratable())
    other = set(EvaluationThresholds.names()) - calibratable

    assert len(calibratable) > len(other)


def test_the_published_definitions_say_the_numbers_are_unvalidated():
    for definition in (ValidationConfig().definition(), EvaluationConfig().definition()):
        assert definition["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"


def test_a_stored_definition_round_trips_back_into_settings():
    original = ReplaySettings()
    rebuilt = ReplaySettings.from_definition(ValidationConfig(settings=original).definition())

    assert rebuilt.as_dict() == original.as_dict()
    for name in ReplaySettings.names():
        assert getattr(rebuilt, name).rationale == getattr(original, name).rationale


def test_an_evaluation_definition_round_trips_too():
    original = EvaluationThresholds()
    rebuilt = EvaluationThresholds.from_definition(
        EvaluationConfig(thresholds=original).definition()
    )

    assert rebuilt.as_dict() == original.as_dict()


def test_changing_a_threshold_changes_the_configuration_checksum():
    """A recalibration is forced to become a new version label, so a report
    produced under old floors stays attributable to them."""
    baseline = EvaluationConfig()
    changed = EvaluationConfig(
        thresholds=EvaluationThresholds(
            min_bucket_sample=EvaluationThreshold(
                value=50.0, kind=CALIBRATABLE, rationale="recalibrated"
            )
        )
    )

    assert baseline.content_checksum() != changed.content_checksum()
    assert baseline.version_label() != changed.version_label()


def test_the_walk_forward_windows_are_thresholds_not_hardcoded_timedeltas():
    thresholds = EvaluationThresholds(
        walk_forward_train_days=EvaluationThreshold(value=90.0, kind=CALIBRATABLE, rationale="test")
    )

    assert thresholds.train_window.days == 90
    assert thresholds.test_window.days == int(EvaluationThresholds().walk_forward_test_days.value)


def test_the_replay_step_is_a_threshold_and_the_chunk_size_is_an_integer():
    settings = ReplaySettings(
        scan_step_days=ValidationSetting(value=3.0, kind=CALIBRATABLE, rationale="test")
    )

    assert settings.step.days == 3
    assert isinstance(ReplaySettings().chunk, int)


def test_the_operational_kind_is_used_only_where_it_is_claimed():
    """Every operational constant must be nameable, so the equivalence
    test in `test_batch_scale.py` can be pointed at all of them."""
    operational = ReplaySettings().operational()

    assert set(operational) == {"batch_size", "max_scan_dates"}
    assert EvaluationThresholds().describe()["calibration_bins"]["kind"] == OPERATIONAL


def test_no_number_is_structural_unless_changing_it_changes_a_definition():
    """A reading of every structural entry, asserted rather than assumed.

    Both structural entries here are counts below which a question has no
    answer — two buckets for a trend, two rolls for a walk-forward — which
    is what `structural` means. If a future entry is tagged structural
    without that property, this list stops matching and the test says so.
    """
    structural = {
        name
        for name, entry in EvaluationThresholds().describe().items()
        if entry["kind"] == STRUCTURAL
    }

    assert structural == {"min_buckets_for_monotonicity", "min_walk_forward_windows"}
