"""Are the weights really in one place, and do they admit what they are?

The same source scan Modules 10 and 12 established, applied to the module
where it matters most: `argus_score` is the number a person acts on, and
every input to it is a guess. If one of those guesses is written inline,
no behavioural test fails and the recalibration Module 17 makes possible
becomes an archaeology exercise.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.scoring import components as components_module
from core.scoring import confidence as confidence_module
from core.scoring import engine as engine_module
from core.scoring import gating as gating_module
from core.scoring import persistence as persistence_module
from core.scoring.config import (
    BRIEF_SHARES,
    CALIBRATABLE,
    STRUCTURAL,
    UNASSIGNED_SHARE,
    ComponentWeight,
    ConfidenceWeights,
    Normalizers,
    ScoringConfig,
    ScoringThresholds,
    ScoringWeights,
    WeightsDoNotSumError,
)

#: Identity elements, index arithmetic, and the bounds of a normalized
#: scale. Anything else in scoring logic is a weight or a threshold.
STRUCTURAL_LITERALS = frozenset({0, 1, -1, 2})

LOGIC_MODULES = (
    components_module,
    confidence_module,
    gating_module,
    engine_module,
    persistence_module,
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
def test_no_numeric_literals_in_scoring_logic(module):
    """Fails the moment a weight or threshold is written inline."""
    source = Path(inspect.getfile(module)).read_text()
    offenders = [
        (line, value)
        for line, value in _numeric_literals(source)
        if value not in STRUCTURAL_LITERALS
    ]
    assert not offenders, (
        f"{module.__name__} contains calibration constants inline: {offenders}. "
        "Every weight, ramp bound and threshold belongs in core/scoring/config.py."
    )


# --------------------------------------------------------------------------
# The open 10%
# --------------------------------------------------------------------------


def test_the_brief_left_ten_percent_unassigned():
    """The premise of the redistribution, asserted rather than assumed."""
    assert sum(BRIEF_SHARES.values()) == pytest.approx(0.90)
    assert pytest.approx(0.10) == UNASSIGNED_SHARE


def test_redistribution_changes_no_pairwise_ratio():
    """Why proportional scaling and not something else.

    It is the only redistribution that adds no information the brief did
    not already contain. Any other split would encode a judgement about
    which component deserves the spare weight — a judgement nothing has
    validated and which would be invisible inside the final numbers.
    """
    weights = ScoringWeights().as_dict()
    active = [name for name, share in BRIEF_SHARES.items() if share > 0]

    for first in active:
        for second in active:
            assert weights[first] / weights[second] == pytest.approx(
                BRIEF_SHARES[first] / BRIEF_SHARES[second]
            )


def test_the_weights_sum_to_one_after_redistribution():
    assert ScoringWeights().total() == pytest.approx(1.0)
    assert sum(ScoringWeights().active().values()) == pytest.approx(1.0)


def test_fundamental_context_stays_at_zero_and_stays_present():
    """Inactive by project decision, structurally present so Module 17 can
    activate it. A component at zero is discoverable; an absent one is not."""
    weights = ScoringWeights()

    assert weights.fundamental_context.value == 0.0
    assert "fundamental_context" in weights.as_dict()
    assert "fundamental_context" not in weights.active()


def test_a_configuration_whose_weights_do_not_sum_to_one_is_rejected():
    """Two configurations that rescale the whole score produce numbers
    nobody can compare, and ranking is the entire point of `argus_score`."""
    with pytest.raises(WeightsDoNotSumError):
        ScoringConfig(
            weights=ScoringWeights(
                pattern_quality=ComponentWeight(value=0.9, kind=CALIBRATABLE, rationale="test")
            )
        )


# --------------------------------------------------------------------------
# Enumerability and honesty
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cls", [ScoringWeights, ConfidenceWeights, ScoringThresholds, Normalizers])
def test_every_number_is_enumerable_and_declares_its_kind(cls):
    """A recalibration tool must find every number, and know how much each
    one is worth trusting, without reading the scoring code."""
    described = cls().describe()

    assert set(described) == set(cls.names())
    for name, entry in described.items():
        assert entry["kind"] in {STRUCTURAL, CALIBRATABLE}, name
        assert entry["rationale"].strip(), f"{name} has no stated reasoning"


def test_most_of_this_module_is_admitted_to_be_invented():
    """An honesty check, asserted rather than left in a docstring.

    Reclassifying invented magnitudes as structural would be the single
    most misleading change anyone could make to the configuration.
    """
    weights = ScoringWeights()
    ramps = Normalizers()

    assert len(weights.calibratable()) > len(ScoringWeights.names()) / 2
    assert len(ramps.calibratable()) > len(Normalizers.names()) / 2


def test_confidence_weights_are_a_separate_set_from_component_weights():
    """Merging them would make it impossible to retune one without
    silently moving the other, and confidence must be able to diverge."""
    assert not set(ScoringWeights.names()) & set(ConfidenceWeights.names())
    assert ConfidenceWeights().total() == pytest.approx(1.0)


def test_the_published_definition_says_the_numbers_are_unvalidated():
    definition = ScoringConfig().definition()

    assert definition["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"
    assert set(definition["weights"]) == set(ScoringWeights.names())
    assert set(definition["normalizers"]) == set(Normalizers.names())
    assert set(definition["confidence_weights"]) == set(ConfidenceWeights.names())


def test_a_stored_definition_round_trips_into_an_identical_configuration():
    """A signal scored years ago must be re-derivable from the
    configuration ID it cites, or the append-only version table records
    that the weights changed without preserving how."""
    original = ScoringConfig()
    rebuilt = ScoringConfig.from_definition(original.definition())

    assert rebuilt.content_checksum() == original.content_checksum()
    assert rebuilt.weights.as_dict() == original.weights.as_dict()
    assert rebuilt.normalizers.as_dict() == original.normalizers.as_dict()
    for name in ScoringWeights.names():
        assert getattr(rebuilt.weights, name).rationale == getattr(original.weights, name).rationale


def test_changing_one_weight_changes_the_configuration_checksum():
    """A recalibration is forced to become a new version row."""
    baseline = ScoringConfig()
    changed = ScoringConfig(
        weights=ScoringWeights(
            pattern_quality=ComponentWeight(
                value=baseline.weights.pattern_quality.value - 0.05,
                kind=CALIBRATABLE,
                rationale="recalibrated",
            ),
            risk_reward=ComponentWeight(
                value=baseline.weights.risk_reward.value + 0.05,
                kind=CALIBRATABLE,
                rationale="recalibrated",
            ),
        )
    )

    assert baseline.content_checksum() != changed.content_checksum()
    assert baseline.version_label() != changed.version_label()


def test_changing_only_a_ramp_also_changes_the_checksum():
    """The normalization is as much a calibration choice as the weight is
    — Module 12's report made that point explicitly — so it has to be
    pinned by the same version identity."""
    from core.scoring.config import Ramp

    baseline = ScoringConfig()
    changed = ScoringConfig(
        normalizers=Normalizers(
            risk_event_proximity=Ramp(low=30.0, high=0.0, kind=CALIBRATABLE, rationale="retuned")
        )
    )
    assert baseline.content_checksum() != changed.content_checksum()
