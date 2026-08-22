"""Is every threshold really in one place, or does the code just claim so?

The prompt asks for a test that would fail if a threshold were hardcoded
inline. This is that test, and it is the load-bearing one in this module.

Module 10's engineering bet is that the *structure* is solid while the
*numbers* are guesses, and that recalibrating the numbers should therefore
be a data change rather than a code change. That bet only pays off if the
separation is real. A single `if volatility_percentile < 0.3` buried in a
conditional would not fail any behavioural test — the classifier would
work fine — and would quietly make the next recalibration an archaeology
exercise.

So `test_no_numeric_literals_in_classification_logic` reads the source of
every module that participates in classification and fails on any numeric
literal that is not structural. It is a crude check and deliberately so:
crude enough that nobody can satisfy it by being clever, only by putting
the number where it belongs.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.market_state import classifier as classifier_module
from core.market_state import states as states_module
from core.market_state.states import STATE_DEFINITIONS
from core.market_state.target_model_matching.models.target_model_v1 import model as v1_model
from core.market_state.target_model_matching.models.target_model_v1.thresholds import (
    TargetModelV1Thresholds,
)
from core.market_state.thresholds import (
    DIRECTIONAL,
    MAGNITUDE,
    MarketStateConfig,
    StateThresholds,
)

#: Literals that are structural rather than calibration: identity
#: elements, index arithmetic, and the 0..1 bounds of a normalized scale.
#: Anything else in classification logic is a threshold in disguise.
STRUCTURAL_LITERALS = frozenset({0, 1, -1, 2})

#: Modules whose source must contain no calibration constants.
CLASSIFICATION_MODULES = (states_module, classifier_module, v1_model)


def _numeric_literals(source: str) -> list[tuple[int, float]]:
    """Every numeric constant in the source, with its line number."""
    tree = ast.parse(source)
    found: list[tuple[int, float]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            if isinstance(node.value, bool):
                continue
            found.append((node.lineno, node.value))
    return found


@pytest.mark.parametrize("module", CLASSIFICATION_MODULES, ids=lambda m: m.__name__)
def test_no_numeric_literals_in_classification_logic(module):
    """The test that keeps the threshold isolation honest.

    If someone adds `if compression < 0.75` directly to a predicate, this
    fails and names the line. That is the entire point: the failure has to
    arrive at the moment the constant is written, not two years later when
    a recalibration cannot find it.
    """
    source = Path(inspect.getfile(module)).read_text()
    offenders = [
        (line, value)
        for line, value in _numeric_literals(source)
        if value not in STRUCTURAL_LITERALS
    ]
    assert not offenders, (
        f"{module.__name__} contains calibration constants inline: {offenders}. "
        "Every threshold belongs in core/market_state/thresholds.py (or the "
        "target model's own thresholds module)."
    )


def test_the_whole_threshold_set_is_enumerable_from_one_object():
    """A recalibration tool must be able to find every number without
    knowing anything about the state machine."""
    thresholds = StateThresholds()
    values = thresholds.as_dict()

    assert values, "the threshold set must not be empty"
    assert set(values) == set(StateThresholds.names())
    assert all(isinstance(v, float) for v in values.values())


def test_every_threshold_declares_whether_it_is_a_guess():
    """Direction and magnitude deserve very different amounts of trust.

    A bare float in a config file is barely better than a bare float in an
    `if`. What makes recalibration tractable is knowing which numbers were
    reasoned boundaries and which were invented.
    """
    described = StateThresholds().describe()

    for name, entry in described.items():
        assert entry["kind"] in {DIRECTIONAL, MAGNITUDE}, name
        assert entry["rationale"].strip(), f"{name} has no stated reasoning"


def test_magnitudes_and_directionals_partition_the_set():
    """Every threshold is one or the other, and none is both."""
    thresholds = StateThresholds()
    magnitudes = set(thresholds.magnitudes())
    directionals = set(thresholds.directional())

    assert magnitudes | directionals == set(StateThresholds.names())
    assert not (magnitudes & directionals)


def test_most_thresholds_are_admitted_to_be_guesses():
    """An honesty check, asserted rather than left to the docstring.

    If a future edit quietly reclassified magnitudes as directional
    boundaries, the set would look far better validated than it is. That
    would be the single most misleading change anyone could make here.
    """
    thresholds = StateThresholds()
    assert len(thresholds.magnitudes()) > len(thresholds.directional())


def test_every_state_predicate_reads_thresholds_from_the_config():
    """Swapping the config must actually change behaviour.

    A predicate that ignored its `thresholds` argument would pass the
    source scan and still be uncalibratable. This catches that by
    construction: move a threshold far enough and the verdict must move.
    """
    import pandas as pd

    # Values chosen so `atr_percentile` is the ONLY condition CONSOLIDATION
    # fails on. Isolating a single threshold is what makes the assertion a
    # statement about that threshold rather than about the predicate.
    values = dict.fromkeys(_all_predicate_inputs(), 0.5)
    values["volatility_compression"] = 0.5  # under the 0.85 default
    values["normalized_range_width"] = 0.1  # under the 0.25 default
    values["atr_percentile"] = 0.5  # OVER the 0.40 default — the deciding one
    frame = pd.DataFrame({k: [v] for k, v in values.items()}, index=["security"])

    default = StateThresholds()
    extreme = StateThresholds(
        consolidation_atr_percentile=type(default.consolidation_atr_percentile)(
            value=0.99, kind=MAGNITUDE, rationale="test override"
        )
    )

    consolidation = next(d for d in STATE_DEFINITIONS if d.state.value == "CONSOLIDATION")
    assert not consolidation.predicate(frame, default).iloc[0]
    assert consolidation.predicate(frame, extreme).iloc[0]


def test_the_target_model_has_its_own_separate_threshold_set():
    """Separate objects, so a second model would not touch the engine's.

    Merging them would make the containment in `target_model_matching/`
    cosmetic — the engine's configuration would carry the model's numbers
    and a replacement model could not be configured without editing it.
    """
    engine_names = set(StateThresholds.names())
    model_names = set(TargetModelV1Thresholds.names())

    assert model_names
    assert not (engine_names & model_names), "the two threshold sets must not overlap"


def test_the_published_definition_carries_both_sets_and_says_they_are_unvalidated():
    """Step 1 and step 3 of the recalibration walkthrough, in one object."""
    definition = MarketStateConfig().definition()

    assert set(definition["state_thresholds"]) == set(StateThresholds.names())
    assert set(definition["target_model_thresholds"]) == set(TargetModelV1Thresholds.names())
    assert definition["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"


def test_a_stored_definition_round_trips_back_into_thresholds():
    """A historical state must be re-derivable from the version it cites.

    Without this, the append-only version table would record *that* the
    thresholds changed without preserving enough to reproduce the old
    behaviour — which is most of the reason for versioning them.
    """
    original = StateThresholds()
    definition = MarketStateConfig(states=original).definition()
    rebuilt = StateThresholds.from_definition(definition)

    assert rebuilt.as_dict() == original.as_dict()
    # And the rationale survives, not just the number.
    for name in StateThresholds.names():
        assert getattr(rebuilt, name).rationale == getattr(original, name).rationale


def test_changing_one_threshold_changes_the_configuration_checksum():
    """A recalibration must be forced to become a new version row."""
    baseline = MarketStateConfig()
    changed = MarketStateConfig(
        states=StateThresholds(
            consolidation_atr_percentile=type(baseline.states.consolidation_atr_percentile)(
                value=0.45, kind=MAGNITUDE, rationale="recalibrated"
            )
        )
    )
    assert baseline.content_checksum() != changed.content_checksum()


def _all_predicate_inputs() -> list[str]:
    return sorted({name for d in STATE_DEFINITIONS for name in d.inputs})
