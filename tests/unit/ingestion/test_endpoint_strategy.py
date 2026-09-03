"""Bulk or per-symbol — decided from what is provisioned, never hardcoded.

The decision matters because getting it wrong is not a slow run, it is a
failed one: calling a bulk endpoint without the plan that includes it
returns an error, and the daily price pull is the half of Module 26 with
a deadline.

These tests pin the inference and, more importantly, its direction of
failure.
"""

from __future__ import annotations

import pytest

from core.ingestion.config import IngestionConfig, IngestionSettings
from core.ingestion.strategy import EndpointStrategy, select_strategy
from packages.config.settings import ProvidersSettings

#: FMP's published per-minute limits, by plan.
STARTER = 300
PREMIUM = 750
ULTIMATE = 3000


def _providers(rate: int) -> ProvidersSettings:
    return ProvidersSettings(fmp_requests_per_minute=rate)


@pytest.mark.parametrize("rate", [STARTER, PREMIUM])
def test_a_plan_below_ultimate_uses_the_per_symbol_path(rate: int):
    """Which is the default, and the one every paid plan supports."""
    decision = select_strategy(_providers(rate))

    assert decision.strategy is EndpointStrategy.PER_SYMBOL
    assert not decision.is_bulk


def test_the_configured_default_is_the_per_symbol_path():
    """The plan ARGUS is recommended to run is Premium, not Ultimate.

    Asserted against the shipped default rather than a constructed value,
    so a change to `ProvidersSettings` that silently switches the daily
    pull to an endpoint the subscription may not include fails here.
    """
    decision = select_strategy(ProvidersSettings())

    assert decision.strategy is EndpointStrategy.PER_SYMBOL


def test_an_ultimate_rate_limit_switches_to_the_single_bulk_request():
    decision = select_strategy(_providers(ULTIMATE))

    assert decision.strategy is EndpointStrategy.BULK
    assert decision.is_bulk


def test_the_threshold_is_configuration_not_a_constant():
    """The day FMP moves bulk to a cheaper plan, one number changes.

    Proven by moving it: with the entitlement threshold lowered to
    Premium's limit, a Premium rate switches to bulk with no code change.
    """
    lowered = IngestionConfig(
        settings=IngestionSettings.from_definition(
            {
                "settings": {
                    **IngestionSettings().as_dict(),
                    "bulk_entitlement_requests_per_minute": float(PREMIUM),
                }
            }
        )
    )

    decision = select_strategy(_providers(PREMIUM), lowered)

    assert decision.strategy is EndpointStrategy.BULK


def test_the_decision_explains_itself_with_both_numbers():
    """It is logged once per run and read when the daily pull surprises somebody."""
    decision = select_strategy(_providers(PREMIUM))

    assert str(PREMIUM) in decision.reason
    assert str(ULTIMATE) in decision.reason
    assert decision.as_dict()["requests_per_minute"] == PREMIUM
    assert decision.as_dict()["bulk_entitlement_threshold"] == ULTIMATE


def test_the_inference_fails_towards_the_path_that_always_works():
    """The asymmetry that makes a proxy acceptable here.

    Being wrong low costs a slower run against an endpoint every plan
    has. Being wrong high costs a failed pull. So every rate below the
    threshold — including nonsense ones — must land on per-symbol.
    """
    for rate in (1, STARTER - 1, ULTIMATE - 1):
        assert select_strategy(_providers(rate)).strategy is EndpointStrategy.PER_SYMBOL
