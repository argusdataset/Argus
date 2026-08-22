"""Tests for AppConfig loading, validation, grouping, and secret-safety."""

import pytest
from pydantic import ValidationError

from packages.config.settings import AppConfig, DatabaseSettings, ProvidersSettings, get_config


def test_missing_database_group_entirely_raises_naming_it():
    with pytest.raises(ValidationError) as exc_info:
        AppConfig()

    assert "database" in str(exc_info.value)


def test_partially_set_database_group_names_each_missing_field(monkeypatch):
    monkeypatch.setenv("ARGUS_DATABASE__HOST", "myhost")

    with pytest.raises(ValidationError) as exc_info:
        AppConfig()

    message = str(exc_info.value)
    assert "database.port" in message
    assert "database.name" in message
    assert "database.user" in message


def test_loads_with_all_required_settings(required_db_env):
    config = AppConfig()
    assert config.database.port == 5432
    assert config.database.name == "argus_test"
    assert config.database.user == "argus_test_user"


def test_database_host_has_a_dev_friendly_default(required_db_env):
    config = AppConfig()
    assert config.database.host == "localhost"


def test_providers_have_defaults(required_db_env):
    config = AppConfig()
    assert config.providers.fmp_base_url == "https://financialmodelingprep.com/api"
    assert config.providers.fmp_request_timeout_seconds == 30


def test_logging_has_defaults(required_db_env):
    config = AppConfig()
    assert config.logging.level == "INFO"
    assert config.logging.format == "text"


def test_database_settings_has_no_password_field():
    assert "password" not in DatabaseSettings.model_fields


def test_providers_settings_has_no_api_key_field():
    assert "fmp_api_key" not in ProvidersSettings.model_fields


def test_get_config_is_cached(required_db_env):
    assert get_config() is get_config()
