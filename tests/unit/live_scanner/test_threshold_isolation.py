"""Are the scanner's numbers in one place, and are they tagged honestly?

The same AST source scan Modules 10 through 17 established. It matters
here for an unusual reason: almost everything in this module's config is
`operational`, which is a tag that can be abused — anything can be called
operational if nobody checks. So the tests below check.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.live_scanner import catchup as catchup_module
from core.live_scanner import daily as daily_module
from core.live_scanner import failures as failures_module
from core.live_scanner import readiness as readiness_module
from core.live_scanner import results as results_module
from core.live_scanner import runs as runs_module
from core.live_scanner import scanner as scanner_module
from core.live_scanner import schedule as schedule_module
from core.live_scanner.config import (
    CALIBRATABLE,
    KINDS,
    OPERATIONAL,
    STRUCTURAL,
    ScannerConfig,
    ScannerSetting,
    ScannerSettings,
)

#: Identity elements and index arithmetic, exactly as Modules 10-17.
STRUCTURAL_LITERALS = frozenset({0, 1, -1, 2})

LOGIC_MODULES = (
    schedule_module,
    readiness_module,
    runs_module,
    failures_module,
    scanner_module,
    catchup_module,
    daily_module,
    results_module,
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
def test_no_numeric_literals_in_scanner_logic(module):
    source = Path(inspect.getfile(module)).read_text()
    offenders = [
        (line, value)
        for line, value in _numeric_literals(source)
        if value not in STRUCTURAL_LITERALS
    ]
    assert not offenders, (
        f"{module.__name__} contains constants inline: {offenders}. Every one belongs "
        "in core/live_scanner/config.py."
    )


def test_the_whole_setting_set_is_enumerable_from_one_object():
    settings = ScannerSettings()
    values = settings.as_dict()

    assert values
    assert set(values) == set(ScannerSettings.names())
    assert all(isinstance(value, float) for value in values.values())


def test_every_setting_declares_one_of_the_three_kinds_and_says_why():
    for name, entry in ScannerSettings().describe().items():
        assert entry["kind"] in KINDS, name
        assert entry["rationale"].strip(), f"{name} has no stated reasoning"


def test_the_two_numbers_that_can_change_an_outcome_are_the_calibratable_ones():
    """The honesty check on the `operational` tag.

    `operational` claims a number cannot change what a scan produces. Two
    numbers here can: the coverage floor decides whether a day is scanned
    at all, and the exclusion budget decides whether a partly-quarantined
    day counts as a scan. Both are calibratable, and nothing else is.
    """
    calibratable = set(ScannerSettings().calibratable())

    assert calibratable == {"min_universe_coverage", "max_excluded_fraction"}


def test_the_operational_numbers_are_only_about_timing_and_effort():
    """Read every operational entry and confirm it is one of those.

    If a future setting is tagged operational without that property, this
    list stops matching and the test says so — which is the only thing
    that makes the tag mean anything.
    """
    operational = {
        name for name, entry in ScannerSettings().describe().items() if entry["kind"] == OPERATIONAL
    }

    assert operational == {
        "scan_offset_hours",
        "retry_interval_minutes",
        "readiness_window_hours",
        "max_attempts",
        "backoff_base_seconds",
        "backoff_max_seconds",
        "max_stored_message_chars",
        "max_listed_exclusions",
        "max_catchup_days",
    }


def test_the_structural_numbers_follow_from_definitions_rather_than_taste():
    """Two of them: attempt numbering starts at zero because the number is
    an index into that date's attempts, and one exclusion is always
    allowed because one security is never a broken feed — which is the
    thing the fraction guards against."""
    structural = {
        name for name, entry in ScannerSettings().describe().items() if entry["kind"] == STRUCTURAL
    }

    assert structural == {"min_attempt_number", "min_excluded_allowance"}


def test_the_published_definition_says_the_numbers_are_unvalidated():
    assert ScannerConfig().definition()["calibration_status"] == "UNVALIDATED_PLACEHOLDERS"


def test_a_stored_definition_round_trips_back_into_settings():
    original = ScannerSettings()
    rebuilt = ScannerSettings.from_definition(ScannerConfig(settings=original).definition())

    assert rebuilt.as_dict() == original.as_dict()
    for name in ScannerSettings.names():
        assert getattr(rebuilt, name).rationale == getattr(original, name).rationale


def test_changing_a_setting_changes_the_configuration_checksum():
    baseline = ScannerConfig()
    changed = ScannerConfig(
        settings=ScannerSettings(
            min_universe_coverage=ScannerSetting(
                value=0.95, kind=CALIBRATABLE, rationale="recalibrated"
            )
        )
    )

    assert baseline.content_checksum() != changed.content_checksum()
    assert baseline.version_label() != changed.version_label()


def test_the_timing_settings_are_read_as_durations_not_raw_floats():
    settings = ScannerSettings(
        scan_offset_hours=ScannerSetting(value=3.0, kind=OPERATIONAL, rationale="test")
    )

    assert settings.scan_offset.total_seconds() == 3 * 60 * 60
    assert settings.retry_interval.total_seconds() == 30 * 60


def test_the_backoff_doubles_and_then_stops_doubling():
    """Exponential so a blip clears fast; capped so a genuine outage is
    not hammered and the scanner gives up in bounded time."""
    settings = ScannerSettings()
    intervals = [settings.backoff_for(n) for n in range(8)]

    assert intervals[:4] == [2.0, 4.0, 8.0, 16.0]
    assert max(intervals) == settings.backoff_max_seconds.value
    assert intervals == sorted(intervals)


def test_the_whole_retry_budget_is_bounded_by_well_under_the_retry_interval():
    """Retries must finish before the next scheduled wake-up, or two runs
    for the same date overlap."""
    settings = ScannerSettings()
    total = sum(settings.backoff_for(n) for n in range(settings.attempts))

    assert total < settings.retry_interval.total_seconds()
