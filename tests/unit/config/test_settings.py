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
    # Host only: FMP's endpoint paths carry their own prefix (/stable/...
    # or the legacy /api/v3/...), so the base URL must not assume one.
    assert config.providers.fmp_base_url == "https://financialmodelingprep.com"
    assert config.providers.fmp_request_timeout_seconds == 30


def test_rate_limits_are_configurable_not_hardcoded(required_db_env, monkeypatch):
    """Limits are a property of the FMP plan, so changing plan is config-only."""
    assert AppConfig().providers.fmp_requests_per_minute == 300

    monkeypatch.setenv("ARGUS_PROVIDERS__FMP_REQUESTS_PER_MINUTE", "3000")
    assert AppConfig().providers.fmp_requests_per_minute == 3000


def test_bulk_endpoints_have_their_own_rate_budget(required_db_env):
    """FMP throttles bulk downloads far harder than standard endpoints."""
    providers = AppConfig().providers
    assert providers.fmp_bulk_requests_per_minute < providers.fmp_requests_per_minute


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
