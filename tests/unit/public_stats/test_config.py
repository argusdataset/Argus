"""The public floor, and the relationship it must keep with Module 17's.

One number in this module decides how much evidence ARGUS demands before
showing a statistic to a stranger. It is the only calibratable value here
and the only one worth testing on its own.
"""

from __future__ import annotations

import pytest

from core.model_validation_evaluation.evaluation.config import EvaluationThresholds
from services.public_stats.config import (
    CALIBRATABLE,
    KINDS,
    PublicStatsConfig,
    PublicStatsSettings,
)


def test_every_setting_declares_a_kind_and_a_rationale():
    for name, entry in PublicStatsSettings().describe().items():
        assert entry["kind"] in KINDS, name
        assert entry["rationale"].strip(), f"{name} has no stated reasoning"


def test_the_calibratable_numbers_are_exactly_the_ones_that_change_the_claim():
    """Everything else bounds how a chart is drawn, never what it says.

    `min_public_sample` decides how much evidence ARGUS demands before
    showing a rate at all. `top_performer_threshold` decides what counts
    as "top" in the one section built to highlight a subset — lowering it
    would dilute that label the same way loosening the public floor would
    dilute a rate. Both are consequential; the display bounds elsewhere
    (bin counts, point limits, list limits) are not.
    """
    assert set(PublicStatsSettings().calibratable()) == {
        "min_public_sample",
        "top_performer_threshold",
    }


def test_the_public_floor_is_never_looser_than_the_internal_one():
    """The relationship that matters, asserted rather than maintained by
    hand.

    Module 17's floor governs an analysis a person reads knowing its
    limits. This one governs a page read by someone with no way to know
    them. If a future edit makes the public floor the looser of the two,
    ARGUS starts publishing figures it would not put in its own internal
    report — which is exactly backwards.
    """
    public = PublicStatsSettings().min_public_sample.value
    internal = EvaluationThresholds().min_bucket_sample.value

    assert public >= internal


def test_the_public_floor_is_admitted_to_be_invented():
    entry = PublicStatsSettings().describe()["min_public_sample"]

    assert entry["kind"] == CALIBRATABLE
    assert "stranger" in entry["rationale"]


def test_the_top_performer_threshold_narrows_success_rather_than_redefining_it():
    entry = PublicStatsSettings().describe()["top_performer_threshold"]

    assert entry["kind"] == CALIBRATABLE
    assert "SUCCESS" in entry["rationale"]
    assert entry["value"] == pytest.approx(0.50)


def test_changing_the_floor_changes_the_configuration_checksum():
    """A recalibration becomes a new version label, so a published figure
    stays attributable to the floor it was published under."""
    from services.public_stats.config import PublicStatsSetting

    baseline = PublicStatsConfig()
    stricter = PublicStatsConfig(
        settings=PublicStatsSettings(
            min_public_sample=PublicStatsSetting(
                value=100.0, kind=CALIBRATABLE, rationale="recalibrated"
            )
        )
    )

    assert baseline.content_checksum() != stricter.content_checksum()
    assert baseline.version_label() != stricter.version_label()


def test_a_line_needs_two_points_and_that_is_structural():
    entry = PublicStatsSettings().describe()["min_points_for_series"]

    assert entry["kind"] == "structural"
    assert entry["value"] == pytest.approx(2.0)


def test_the_evaluation_config_inherits_the_public_floor_rather_than_copying_it():
    """One public number. Module 17's thresholds are built from it at use
    time, so the two cannot drift."""
    from services.public_stats.aggregates import _evaluation_config
    from services.public_stats.config import PublicStatsSetting

    config = PublicStatsConfig(
        settings=PublicStatsSettings(
            min_public_sample=PublicStatsSetting(value=77.0, kind=CALIBRATABLE, rationale="test")
        )
    )
    derived = _evaluation_config(config)

    assert derived.thresholds.min_bucket_sample.value == pytest.approx(77.0)
    assert derived.thresholds.min_regime_sample.value == pytest.approx(77.0)
