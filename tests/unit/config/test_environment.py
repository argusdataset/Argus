"""Tests for the Environment enum and AppConfig's environment field."""

import pytest
from packages.config.environment import Environment
from packages.config.settings import AppConfig
from pydantic import ValidationError


def test_defaults_to_development(required_db_env):
    config = AppConfig()
    assert config.environment is Environment.DEVELOPMENT


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("development", Environment.DEVELOPMENT),
        ("staging", Environment.STAGING),
        ("production", Environment.PRODUCTION),
    ],
)
def test_loads_each_known_environment(monkeypatch, required_db_env, value, expected):
    monkeypatch.setenv("ARGUS_ENV", value)
    config = AppConfig()
    assert config.environment is expected


def test_invalid_environment_raises_rather_than_defaulting(monkeypatch, required_db_env):
    monkeypatch.setenv("ARGUS_ENV", "not-a-real-environment")
    with pytest.raises(ValidationError):
        AppConfig()
