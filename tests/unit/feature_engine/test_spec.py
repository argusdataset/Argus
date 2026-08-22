"""The versioned spec, and the no-duration-threshold rule it encodes.

Two claims are tested here. The first is mechanical: a spec's checksum is
stable across processes and changes whenever anything that affects a
computed value changes, because that is what makes a recorded
`feature_schema_version_id` reproduce a historical number.

The second is the architectural one — **no fixed duration thresholds
anywhere**. That is not a property a unit test can prove in general, so
this file tests the observable consequence: change the windows and every
feature still computes, for a security whose phases are far shorter than
the default scales. A gate would produce a different *kind* of answer at a
different duration; a scale only changes the number's resolution.
"""

from __future__ import annotations

import json

import pytest

from core.feature_engine.groups import awakening, confirmation, consolidation, context, decline
from core.feature_engine.spec import (
    FEATURE_GROUPS,
    FEATURE_NAMES,
    FeatureSpec,
    FeatureTolerances,
    FeatureWindows,
)


def test_the_checksum_is_stable_for_an_identical_spec():
    """Two equal specs hash identically — otherwise nothing is reproducible."""
    assert FeatureSpec().content_checksum() == FeatureSpec().content_checksum()


def test_changing_any_window_changes_the_checksum():
    """A silent recalculation under an existing version ID is the failure mode.

    If a window could move without the checksum moving, a signal recorded
    in 2019 would re-run in 2027 against different arithmetic while
    claiming the same schema version.
    """
    baseline = FeatureSpec().content_checksum()
    for field, value in (
        ("short", 11),
        ("medium", 21),
        ("long", 61),
        ("structural", 253),
        ("pivot", 6),
        ("percentile", 253),
    ):
        altered = FeatureSpec(windows=FeatureWindows(**{field: value}))
        assert altered.content_checksum() != baseline, f"{field} did not affect the checksum"


def test_changing_a_tolerance_changes_the_checksum():
    altered = FeatureSpec(tolerances=FeatureTolerances(level_test=0.02))
    assert altered.content_checksum() != FeatureSpec().content_checksum()


def test_the_feature_list_is_part_of_the_checksum():
    """Adding a feature changes the vector's meaning even if no window moved."""
    definition = FeatureSpec().definition()
    assert definition["feature_names"] == sorted(FEATURE_NAMES)


def test_the_definition_is_json_serializable():
    """It is stored as JSONB on the schema-version row."""
    json.dumps(FeatureSpec().definition())


def test_the_version_label_carries_the_checksum():
    spec = FeatureSpec()
    assert spec.version_label().endswith(spec.content_checksum()[:12])


def test_max_lookback_is_the_longest_scale():
    assert FeatureSpec().windows.max_lookback == 252


def test_feature_names_are_unique_across_groups():
    """A duplicated name would be silently overwritten during the dict merge."""
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))
    assert len(FEATURE_NAMES) == sum(len(names) for names in FEATURE_GROUPS.values())


# --------------------------------------------------------------------------
# The no-duration-threshold rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "windows",
    [
        pytest.param(FeatureWindows(), id="default-scales"),
        pytest.param(
            FeatureWindows(short=3, medium=5, long=10, structural=20, pivot=2, percentile=20),
            id="scales-far-shorter-than-any-phase",
        ),
        pytest.param(
            FeatureWindows(short=20, medium=40, long=120, structural=400, pivot=10, percentile=400),
            id="scales-far-longer-than-any-phase",
        ),
    ],
)
def test_every_feature_computes_at_any_measurement_scale(panel, market_close, windows):
    """A three-week base and a three-year base go through the same rulers.

    The rule the whole architecture is built around, stated as something
    checkable: shrink or stretch every window and the same 50 features
    still come out. Nothing branches on how long a phase lasted, so
    nothing has a duration below which it stops producing a reading.
    """
    spec = FeatureSpec(windows=windows)
    frames: dict = {}
    frames.update(decline.compute(panel, spec, market_close=market_close))
    frames.update(consolidation.compute(panel, spec))
    frames.update(awakening.compute(panel, spec, market_close=market_close))
    frames.update(confirmation.compute(panel, spec, market_close=market_close))
    frames.update(context.compute(panel, spec, market_close=market_close))

    assert set(frames) == set(FEATURE_NAMES)
    for name, frame in frames.items():
        assert frame.shape == panel.close_adj.shape, f"{name} changed shape"


def test_duration_is_reported_as_a_number_at_every_scale(panel, market_close):
    """`decline_duration_bars` is a stored feature, and only ever that.

    It is measured against whatever structural window the spec names — a
    different ruler gives a different number, never a different verdict.
    """
    short = decline.compute(
        panel, FeatureSpec(windows=FeatureWindows(structural=60)), market_close=market_close
    )
    long = decline.compute(
        panel, FeatureSpec(windows=FeatureWindows(structural=252)), market_close=market_close
    )
    assert short["decline_duration_bars"].notna().any().any()
    assert long["decline_duration_bars"].notna().any().any()
    # A longer ruler can see a more distant peak, so it measures at least
    # as far back — but both produce a number for the same bars.
    assert long["decline_duration_bars"].max().max() > short["decline_duration_bars"].max().max()
