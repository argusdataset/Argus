"""Typed, validated configuration — the single place other modules get settings.

Usage:
    from packages.config import get_config
    config = get_config()

Secret values (passwords, API keys) are never on the config object itself —
see packages.config.secrets / get_secrets_provider() for how to resolve
those.
"""

from packages.config.environment import Environment
from packages.config.execution import ExecutionMode
from packages.config.secrets import (
    ChainedSecretsProvider,
    DotEnvSecretsProvider,
    EnvironmentSecretsProvider,
    SecretNotFoundError,
    SecretsProvider,
    get_secrets_provider,
)
from packages.config.settings import AppConfig, get_config

__all__ = [
    "AppConfig",
    "ChainedSecretsProvider",
    "DotEnvSecretsProvider",
    "EnvironmentSecretsProvider",
    "Environment",
    "ExecutionMode",
    "SecretNotFoundError",
    "SecretsProvider",
    "get_config",
    "get_secrets_provider",
]
