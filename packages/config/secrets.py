"""The secrets-resolution seam.

All secret access in ARGUS goes through SecretsProvider. No module should
ever read a raw credential from os.environ or a config field directly. This
module defines the interface and the only concrete implementation that
exists so far: reading from a local .env file, for development use.

A real secrets manager (AWS Secrets Manager, Vault, ...) is a later
infrastructure decision. Adding one means adding a new SecretsProvider
implementation here — nothing else in the codebase should need to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from dotenv import dotenv_values

from packages.config.settings import AppConfig, get_config


class SecretNotFoundError(KeyError):
    """Raised when a requested secret key has no value in the active provider."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key

    def __str__(self) -> str:
        return f"Secret '{self.key}' was not found."


class SecretsProvider(ABC):
    """Resolves secret values by key. Never expose a resolved value in logs or reprs."""

    @abstractmethod
    def get_secret(self, key: str) -> str:
        """Return the secret value stored under key, or raise SecretNotFoundError."""


class DotEnvSecretsProvider(SecretsProvider):
    """Reads secrets from a local .env file. Development use only.

    Values are loaded once at construction, not re-read on every call.
    """

    def __init__(self, dotenv_path: str | Path = ".env") -> None:
        self._dotenv_path = Path(dotenv_path)
        self._values: dict[str, str] = {
            key: value
            for key, value in dotenv_values(self._dotenv_path).items()
            if value is not None
        }

    def get_secret(self, key: str) -> str:
        try:
            return self._values[key]
        except KeyError:
            raise SecretNotFoundError(key) from None

    def __repr__(self) -> str:
        return f"DotEnvSecretsProvider(dotenv_path={str(self._dotenv_path)!r}, keys_loaded={len(self._values)})"


def get_secrets_provider(config: AppConfig | None = None) -> SecretsProvider:
    """Build the active SecretsProvider from config.

    Only one implementation exists today. This is the one place backend
    selection will happen once a real secrets manager is added for
    staging/production — nothing that calls get_secret() needs to change.
    """
    cfg = config or get_config()
    return DotEnvSecretsProvider(cfg.secrets.dotenv_path)
