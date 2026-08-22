"""The connection layer reads config from Module 02 and the password from secrets."""

from __future__ import annotations

import pytest

from infra.db.connection import DATABASE_PASSWORD_SECRET, build_database_url
from packages.config.secrets import SecretNotFoundError, SecretsProvider
from packages.config.settings import AppConfig

REQUIRED_DB_ENV = {
    "ARGUS_DATABASE__HOST": "db.internal",
    "ARGUS_DATABASE__PORT": "6543",
    "ARGUS_DATABASE__NAME": "argus_prod",
    "ARGUS_DATABASE__USER": "argus_app",
}

SECRET_PASSWORD = "correct-horse-battery-staple"


class StubSecretsProvider(SecretsProvider):
    """In-memory provider, so these tests need no .env file and no database."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values if values is not None else {DATABASE_PASSWORD_SECRET: SECRET_PASSWORD}

    def get_secret(self, key: str) -> str:
        try:
            return self._values[key]
        except KeyError:
            raise SecretNotFoundError(key) from None


@pytest.fixture
def config(monkeypatch) -> AppConfig:
    for key, value in REQUIRED_DB_ENV.items():
        monkeypatch.setenv(key, value)
    return AppConfig()


def test_url_is_built_from_database_settings(config: AppConfig):
    url = build_database_url(config, StubSecretsProvider())

    assert url.host == "db.internal"
    assert url.port == 6543
    assert url.database == "argus_prod"
    assert url.username == "argus_app"


def test_url_targets_postgresql_via_psycopg(config: AppConfig):
    assert build_database_url(config, StubSecretsProvider()).drivername == "postgresql+psycopg"


def test_password_comes_from_the_secrets_provider(config: AppConfig):
    """It is deliberately not a config field — see Module 02's report."""
    url = build_database_url(config, StubSecretsProvider())
    assert url.password == SECRET_PASSWORD


def test_password_is_not_a_config_field(config: AppConfig):
    assert "password" not in type(config.database).model_fields


def test_password_does_not_leak_into_the_url_repr(config: AppConfig):
    """An accidentally logged URL object must not expose the credential."""
    url = build_database_url(config, StubSecretsProvider())

    assert SECRET_PASSWORD not in repr(url)
    assert SECRET_PASSWORD not in str(url)
    assert SECRET_PASSWORD not in url.render_as_string()
    # Only the explicit, deliberate call exposes it.
    assert SECRET_PASSWORD in url.render_as_string(hide_password=False)


def test_missing_database_password_raises_clearly(config: AppConfig):
    with pytest.raises(SecretNotFoundError) as exc_info:
        build_database_url(config, StubSecretsProvider({}))

    assert DATABASE_PASSWORD_SECRET in str(exc_info.value)
