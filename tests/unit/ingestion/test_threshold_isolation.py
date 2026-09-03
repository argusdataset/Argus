"""Are Module 26's numbers in one place, and is the new kind label honest?

The same AST source scan Modules 10 through 18 established, plus one
question specific to this module: it introduces a fourth kind of number,
`cost_policy`, and a kind that nobody checks is a kind anybody can abuse.

The claim `cost_policy` makes is narrow — *this is a declared spending
decision, and no amount of outcome data could validate it.* These tests
enumerate every setting carrying the tag and confirm the claim holds of
each, so that a calibratable threshold cannot be parked here to escape
recalibration.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.ingestion import deep_refresh as deep_refresh_module
from core.ingestion import members as members_module
from core.ingestion import orchestrator as orchestrator_module
from core.ingestion import phases as phases_module
from core.ingestion import prices as prices_module
from core.ingestion import refresh_log as refresh_log_module
from core.ingestion import strategy as strategy_module
from core.ingestion import tiers as tiers_module
from core.ingestion.config import (
    COST_POLICY,
    COST_POLICY_BASIS,
    INGESTION_KINDS,
    OPERATIONAL,
    TIER_SETTINGS,
    IngestionConfig,
    IngestionSettings,
)
from core.market_state.watchlists import WATCHLIST_NAMES
from core.model_validation_evaluation.validation.config import CALIBRATABLE, KINDS

#: Identity elements and index arithmetic, exactly as Modules 10-18.
STRUCTURAL_LITERALS = frozenset({0, 1, -1, 2})

LOGIC_MODULES = (
    tiers_module,
    strategy_module,
    phases_module,
    members_module,
    refresh_log_module,
    prices_module,
    deep_refresh_module,
    orchestrator_module,
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
def test_no_numeric_literals_in_ingestion_logic(module):
    source = Path(inspect.getfile(module)).read_text()
    offenders = [
        (line, value)
        for line, value in _numeric_literals(source)
        if value not in STRUCTURAL_LITERALS
    ]
    assert not offenders, (
        f"{module.__name__} contains constants inline: {offenders}. Every one belongs "
        "in core/ingestion/config.py."
    )


def test_the_whole_setting_set_is_enumerable_from_one_object():
    settings = IngestionSettings()
    values = settings.as_dict()

    assert values
    assert set(values) == set(IngestionSettings.names())
    assert all(isinstance(value, float) for value in values.values())


def test_every_setting_declares_a_known_kind_and_says_why():
    for name, entry in IngestionSettings().describe().items():
        assert entry["kind"] in INGESTION_KINDS, name
        assert entry["rationale"].strip(), f"{name} has no stated reasoning"


def test_the_new_kind_extends_the_three_rather_than_replacing_them():
    """`cost_policy` is a fourth kind, not a rename of an existing one."""
    assert set(KINDS).issubset(set(INGESTION_KINDS))
    assert COST_POLICY not in KINDS
    assert set(INGESTION_KINDS) - set(KINDS) == {COST_POLICY}


def test_the_cost_policy_numbers_are_exactly_the_tiers_and_the_plan_threshold():
    """The honesty check on the new tag.

    `cost_policy` claims a number is a spending decision no outcome data
    could validate. Four tier intervals and one plan-entitlement
    threshold are that; anything else appearing here later has to
    justify itself against this list.
    """
    assert set(IngestionSettings().cost_policy()) == {
        "down_trend_interval_days",
        "consolidation_interval_days",
        "breakout_ready_interval_days",
        "uptrend_interval_days",
        "bulk_entitlement_requests_per_minute",
    }


def test_nothing_in_this_module_is_calibratable():
    """A fetch scheduler owns no judgement that outcome data could settle.

    If a number here ever is calibratable it belongs to the module whose
    judgement it encodes, not to this one — so this asserts the absence
    rather than leaving it to be noticed.
    """
    kinds = {entry["kind"] for entry in IngestionSettings().describe().values()}
    assert CALIBRATABLE not in kinds
    assert kinds == {COST_POLICY, OPERATIONAL}


def test_the_definition_does_not_claim_to_be_an_unvalidated_placeholder():
    """The label means "not yet fitted to outcome data". These never will be.

    Every other versioned config in ARGUS stamps
    `calibration_status: UNVALIDATED_PLACEHOLDERS`, and that label is
    worth something precisely because it identifies a queue of numbers
    waiting for real data. A spending decision put in that queue can
    never leave it, and dilutes the label for everything that can.
    """
    definition = IngestionConfig().definition()

    assert definition["basis"] == COST_POLICY_BASIS
    assert "calibration_status" not in definition
    assert "UNVALIDATED_PLACEHOLDERS" not in str(definition)


def test_every_watchlist_module_ten_publishes_has_a_tier():
    """A watchlist with no tier would be a list nobody ever refreshes.

    Read from Module 10's own names rather than restated, so adding a
    fifth watchlist there fails here — which is the point: the failure
    should be a test, not a set of securities silently going stale.
    """
    assert set(TIER_SETTINGS) == set(WATCHLIST_NAMES)


def test_the_tier_map_points_at_settings_that_exist():
    settings = IngestionSettings()
    for watchlist, setting_name in TIER_SETTINGS.items():
        assert setting_name in IngestionSettings.names(), watchlist
        assert settings.interval_days(watchlist) >= 1


def test_an_unknown_watchlist_raises_rather_than_defaulting():
    """A default interval would silently invent a refresh cadence."""
    with pytest.raises(KeyError, match="No refresh tier"):
        IngestionSettings().interval_days("MOMENTUM")


def test_the_version_label_changes_when_a_tier_changes():
    """A run records which policy was in force; the label has to move."""
    baseline = IngestionConfig()
    changed = IngestionConfig(
        settings=IngestionSettings.from_definition(
            {"settings": {**baseline.settings.as_dict(), "consolidation_interval_days": 5.0}}
        )
    )

    assert baseline.version_label() != changed.version_label()
    assert baseline.content_checksum() == IngestionConfig().content_checksum()


def test_the_version_label_changes_when_the_statement_set_changes():
    """Fetching three statement types instead of five is a different policy."""
    baseline = IngestionConfig()
    trimmed = IngestionConfig(statement_types=("INCOME_STATEMENT",))

    assert baseline.version_label() != trimmed.version_label()
