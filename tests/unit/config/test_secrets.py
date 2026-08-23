"""Tests for the secrets-resolution seam."""

import pytest

from packages.config.secrets import (
    ChainedSecretsProvider,
    DotEnvSecretsProvider,
    EnvironmentSecretsProvider,
    SecretNotFoundError,
    get_secrets_provider,
)
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


# --------------------------------------------------------------------------
# Environment-backed secrets (added during the deployment-readiness audit)
# --------------------------------------------------------------------------


def test_reads_secret_from_the_process_environment(monkeypatch):
    """The gap this closes: hosting platforms inject secrets as
    environment variables, and until this existed ARGUS could not see them
    at all — `DotEnvSecretsProvider` reads a file and nothing else."""
    monkeypatch.setenv("FMP_API_KEY", "value-from-environment")

    assert EnvironmentSecretsProvider().get_secret("FMP_API_KEY") == "value-from-environment"


def test_missing_environment_secret_raises_the_same_error(monkeypatch):
    """Same exception type as the .env provider, so a caller handles one
    failure mode rather than two."""
    monkeypatch.delenv("FMP_API_KEY", raising=False)

    with pytest.raises(SecretNotFoundError):
        EnvironmentSecretsProvider().get_secret("FMP_API_KEY")


def test_the_environment_is_read_at_call_time_not_snapshotted(monkeypatch):
    """A process manager may rewrite the environment between calls, and a
    stale snapshot would be indistinguishable from a missing secret."""
    provider = EnvironmentSecretsProvider()
    monkeypatch.delenv("LATE_BOUND_SECRET", raising=False)

    with pytest.raises(SecretNotFoundError):
        provider.get_secret("LATE_BOUND_SECRET")

    monkeypatch.setenv("LATE_BOUND_SECRET", "arrived-later")
    assert provider.get_secret("LATE_BOUND_SECRET") == "arrived-later"


def test_environment_provider_repr_carries_no_values(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "must-not-appear")

    assert "must-not-appear" not in repr(EnvironmentSecretsProvider())


# --------------------------------------------------------------------------
# The chain, and its deliberate ordering
# --------------------------------------------------------------------------


def test_the_dotenv_file_wins_over_the_environment(tmp_path, monkeypatch):
    """The load-bearing ordering assertion.

    `.env` is consulted first so that every existing local setup resolves
    exactly as it did before the chain existed. The reverse order would be
    a silent behaviour change: a stray exported variable in a developer's
    shell would begin overriding their `.env`.
    """
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("FMP_API_KEY=from-dotenv\n")
    monkeypatch.setenv("FMP_API_KEY", "from-environment")

    provider = ChainedSecretsProvider(
        [DotEnvSecretsProvider(dotenv_file), EnvironmentSecretsProvider()]
    )

    assert provider.get_secret("FMP_API_KEY") == "from-dotenv"


def test_the_environment_fills_in_what_the_dotenv_lacks(tmp_path, monkeypatch):
    """Which in production is everything: a deployed image carries no
    `.env`, because it is gitignored and never committed."""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("FMP_API_KEY=from-dotenv\n")
    monkeypatch.setenv("DATABASE_PASSWORD", "from-environment")

    provider = ChainedSecretsProvider(
        [DotEnvSecretsProvider(dotenv_file), EnvironmentSecretsProvider()]
    )

    assert provider.get_secret("FMP_API_KEY") == "from-dotenv"
    assert provider.get_secret("DATABASE_PASSWORD") == "from-environment"


def test_a_secret_in_neither_source_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("NOWHERE_SECRET", raising=False)
    provider = ChainedSecretsProvider(
        [DotEnvSecretsProvider(tmp_path / "absent.env"), EnvironmentSecretsProvider()]
    )

    with pytest.raises(SecretNotFoundError, match="NOWHERE_SECRET"):
        provider.get_secret("NOWHERE_SECRET")


def test_an_empty_chain_is_refused_at_construction():
    """A chain with no links resolves nothing and would fail as though
    every secret were missing."""
    with pytest.raises(ValueError, match="at least one"):
        ChainedSecretsProvider([])


def test_chain_repr_carries_no_values(tmp_path, monkeypatch):
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("FMP_API_KEY=must-not-appear\n")
    monkeypatch.setenv("DATABASE_PASSWORD", "also-must-not-appear")

    text = repr(
        ChainedSecretsProvider([DotEnvSecretsProvider(dotenv_file), EnvironmentSecretsProvider()])
    )

    assert "must-not-appear" not in text
    assert "also-must-not-appear" not in text


def test_the_default_provider_is_the_dotenv_then_environment_chain(
    tmp_path, monkeypatch, required_db_env
):
    """The factory is still the one place backend selection happens."""
    dotenv_file = tmp_path / "custom.env"
    dotenv_file.write_text("FMP_API_KEY=from-dotenv\n")
    monkeypatch.setenv("ARGUS_SECRETS__DOTENV_PATH", str(dotenv_file))
    monkeypatch.setenv("DATABASE_PASSWORD", "from-environment")

    provider = get_secrets_provider(get_config())

    assert isinstance(provider, ChainedSecretsProvider)
    assert provider.get_secret("FMP_API_KEY") == "from-dotenv"
    assert provider.get_secret("DATABASE_PASSWORD") == "from-environment"
