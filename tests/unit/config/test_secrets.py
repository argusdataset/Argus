"""Tests for the secrets-resolution seam."""

import pytest

from packages.config.secrets import DotEnvSecretsProvider, SecretNotFoundError, get_secrets_provider
from packages.config.settings import get_config


def test_reads_secret_from_dotenv_file(tmp_path):
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("FMP_API_KEY=super-secret-value-123\n")

    provider = DotEnvSecretsProvider(dotenv_file)

    assert provider.get_secret("FMP_API_KEY") == "super-secret-value-123"


def test_missing_secret_raises_secret_not_found_error(tmp_path):
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("SOME_OTHER_KEY=value\n")

    provider = DotEnvSecretsProvider(dotenv_file)

    with pytest.raises(SecretNotFoundError):
        provider.get_secret("FMP_API_KEY")


def test_missing_dotenv_file_does_not_raise_at_construction(tmp_path):
    provider = DotEnvSecretsProvider(tmp_path / "does-not-exist.env")

    with pytest.raises(SecretNotFoundError):
        provider.get_secret("FMP_API_KEY")


def test_secret_value_does_not_leak_into_repr_or_str(tmp_path):
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("FMP_API_KEY=super-secret-value-123\n")

    provider = DotEnvSecretsProvider(dotenv_file)

    assert "super-secret-value-123" not in repr(provider)
    assert "super-secret-value-123" not in str(provider)


def test_get_secrets_provider_uses_configured_dotenv_path(tmp_path, monkeypatch, required_db_env):
    dotenv_file = tmp_path / "custom.env"
    dotenv_file.write_text("FMP_API_KEY=value-from-custom-path\n")
    monkeypatch.setenv("ARGUS_SECRETS__DOTENV_PATH", str(dotenv_file))

    provider = get_secrets_provider(get_config())

    assert provider.get_secret("FMP_API_KEY") == "value-from-custom-path"
