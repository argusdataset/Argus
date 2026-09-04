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

import os
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path

from dotenv import dotenv_values

from packages.config.settings import AppConfig, SecretsSettings, get_config


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


class EnvironmentSecretsProvider(SecretsProvider):
    """Reads secrets from the process environment.

    Hosting platforms — Railway, Fly, Heroku, ECS, Kubernetes — inject
    secrets as environment variables. Until this existed, ARGUS could not
    see them at all: `DotEnvSecretsProvider` reads a `.env` *file* and
    nothing else, so a deployed process had no way to resolve
    `DATABASE_PASSWORD` or `FMP_API_KEY`.

    Read at call time rather than snapshotted at construction, because a
    process manager may rewrite the environment between calls and a stale
    snapshot would be indistinguishable from a missing secret.
    """

    def get_secret(self, key: str) -> str:
        try:
            return os.environ[key]
        except KeyError:
            raise SecretNotFoundError(key) from None

    def __repr__(self) -> str:
        return "EnvironmentSecretsProvider()"


class ChainedSecretsProvider(SecretsProvider):
    """Tries each provider in order and returns the first hit.

    Order is deliberate and load-bearing: the local `.env` is consulted
    **before** the environment, so a developer's existing setup resolves
    exactly as it did before this class existed. The environment fills in
    only what `.env` does not have — which in production is everything,
    since a deployed image carries no `.env` (it is gitignored and never
    committed).

    The reverse order would be a silent behaviour change: a stray exported
    variable in a developer's shell would begin overriding their `.env`.
    """

    def __init__(self, providers: Sequence[SecretsProvider]) -> None:
        if not providers:
            raise ValueError("ChainedSecretsProvider needs at least one provider.")
        self._providers = tuple(providers)

    def get_secret(self, key: str) -> str:
        for provider in self._providers:
            try:
                return provider.get_secret(key)
            except SecretNotFoundError:
                continue
        raise SecretNotFoundError(key)

    def __repr__(self) -> str:
        inner = ", ".join(repr(provider) for provider in self._providers)
        return f"ChainedSecretsProvider([{inner}])"


def bootstrap_secrets_provider() -> SecretsProvider:
    """A provider that works before `AppConfig` is known to be loadable.

    `get_secrets_provider` reads `cfg.secrets.dotenv_path`, so it needs a
    valid `AppConfig`. This existed because a deployment supplying only
    `DATABASE_URL` did not have one: `AppConfig.database` was required and
    a connection string satisfied none of its discrete fields — G3 in
    `docs/architecture/KNOWN_ISSUES.md`, and exactly how ARGUS's first
    Railway deploy crashed.

    **G3 is now fixed at the root.** `AppConfig` derives the `database`
    group from `DATABASE_URL`, so in a deployment that supplies one this
    function's fallback no longer fires — `get_secrets_provider()` simply
    succeeds and returns the same chain. It is kept rather than deleted
    because the guarantee it makes is broader than that one cause: a
    caller needing a secret before it can be *sure* the config loads
    still has a way to get one, whatever makes the config unloadable
    (a malformed value, a missing group with no URL to derive it from).

    The fallback is the default chain (`.env`, then the process
    environment) with the default dotenv path, which is what
    `get_secrets_provider` would have produced anyway in every deployment
    that has not overridden `ARGUS_SECRETS__DOTENV_PATH`.

    Prefer `get_secrets_provider` wherever a config is already in hand.
    """
    try:
        return get_secrets_provider()
    except Exception:  # noqa: BLE001 - any config failure means "fall back"
        return ChainedSecretsProvider(
            [DotEnvSecretsProvider(SecretsSettings().dotenv_path), EnvironmentSecretsProvider()]
        )


def get_secrets_provider(config: AppConfig | None = None) -> SecretsProvider:
    """Build the active SecretsProvider from config.

    The local `.env` file first, then the process environment. Local
    development and CI are unaffected — anything resolving from `.env`
    before still resolves from `.env`, first — while a deployed process,
    which has no `.env`, resolves everything from injected environment
    variables.

    This is still the one place backend selection happens; adding a real
    secrets manager (AWS Secrets Manager, Vault) means adding an
    implementation above and another link in this chain, and nothing that
    calls get_secret() changes.
    """
    cfg = config or get_config()
    return ChainedSecretsProvider(
        [DotEnvSecretsProvider(cfg.secrets.dotenv_path), EnvironmentSecretsProvider()]
    )
