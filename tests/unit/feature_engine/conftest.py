"""Fixtures for the pure-computation half of Module 08's tests.

No database here. These tests are about whether the arithmetic is right —
whether `volatility_compression` actually falls when volatility
compresses — and a real Postgres would add nothing but runtime. The
PIT-correctness and MissReason tests, which *are* claims about what the
database returns, live in `tests/integration/feature_engine/`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.feature_engine.panel import PricePanel
from core.feature_engine.spec import FeatureSpec
from tests.unit.feature_engine.lifecycle import MARKET_ID, build_lifecycle_panel


@pytest.fixture(scope="module")
def lifecycle() -> tuple[PricePanel, dict[str, int]]:
    """The synthetic setup panel and its phase-end row indices.

    Module-scoped: the generator is deterministic and nothing mutates the
    panel, so rebuilding it per test would only cost time.
    """
    return build_lifecycle_panel()


@pytest.fixture(scope="module")
def panel(lifecycle: tuple[PricePanel, dict[str, int]]) -> PricePanel:
    return lifecycle[0]


@pytest.fixture(scope="module")
def phase(lifecycle: tuple[PricePanel, dict[str, int]]) -> dict[str, int]:
    return lifecycle[1]


@pytest.fixture(scope="module")
def market_close(panel: PricePanel) -> pd.Series:
    return panel.close_adj[MARKET_ID]


@pytest.fixture(scope="module")
def spec() -> FeatureSpec:
    return FeatureSpec()
