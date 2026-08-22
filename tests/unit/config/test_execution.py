"""Tests for the ExecutionMode enum on AppConfig."""

from packages.config.execution import ExecutionMode
from packages.config.settings import AppConfig


def test_defaults_to_live(required_db_env):
    config = AppConfig()
    assert config.execution.mode is ExecutionMode.LIVE


def test_parses_historical_batch(monkeypatch, required_db_env):
    monkeypatch.setenv("ARGUS_EXECUTION__MODE", "historical_batch")
    config = AppConfig()
    assert config.execution.mode is ExecutionMode.HISTORICAL_BATCH
