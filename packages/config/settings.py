"""Typed, validated application configuration.

Config is exposed to the rest of ARGUS as a single typed AppConfig object,
obtained via get_config() — never as scattered os.environ / raw dict
lookups. Fields are grouped by domain (database, providers, execution,
logging, secrets) rather than flattened, so each future module only needs
to look at the group relevant to it.

Values are read from environment variables, optionally loaded from a local
.env file for development. Nested groups use a double-underscore delimiter,
e.g. ARGUS_DATABASE__PORT. The environment selector is the one exception:
it's always ARGUS_ENV, never ARGUS_ENVIRONMENT, per the module's spec.

Secret VALUES (database passwords, the FMP API key, ...) are never fields
on this object — see secrets.py. AppConfig only ever holds settings *about*
secret resolution (e.g. where the local .env file is), never a secret
itself, so it's always safe to log or repr.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.config.environment import Environment
from packages.config.execution import ExecutionMode


class DatabaseSettings(BaseModel):
    """Connection settings Module 03 (Database Foundation) will use.

    No password field — the database credential is a secret, resolved at
    connection time via SecretsProvider, never stored on this object.
    """

    host: str = "localhost"
    port: int
    name: str
    user: str


class ProvidersSettings(BaseModel):
    """External data provider settings. Module 04 (FMP Provider Adapter) will use these.

    No API key field — the FMP credential is a secret, resolved via
    SecretsProvider, never stored on this object.
    """

    fmp_base_url: str = "https://financialmodelingprep.com/api"
    fmp_request_timeout_seconds: int = 30


class ExecutionSettings(BaseModel):
    """Which run mode the Intelligence Core is in. See ExecutionMode."""

    mode: ExecutionMode = ExecutionMode.LIVE


class LoggingSettings(BaseModel):
    """Logging fields. Module 23 (Observability) wires up the actual logging framework."""

    level: str = "INFO"
    format: str = "text"


class SecretsSettings(BaseModel):
    """Settings about how secrets are resolved — never a secret value itself.

    Only a local .env-backed provider exists today (DotEnvSecretsProvider,
    in secrets.py). A real secrets manager is a later infrastructure
    decision.
    """

    dotenv_path: str = ".env"


class AppConfig(BaseSettings):
    """The single typed, validated ARGUS configuration object.

    Load with get_config(), not by instantiating AppConfig() directly
    outside of tests, so the process-wide config is loaded and validated
    exactly once.
    """

    model_config = SettingsConfigDict(
        env_prefix="ARGUS_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Field(default=Environment.DEVELOPMENT, validation_alias="ARGUS_ENV")
    database: DatabaseSettings
    providers: ProvidersSettings = ProvidersSettings()
    execution: ExecutionSettings = ExecutionSettings()
    logging: LoggingSettings = LoggingSettings()
    secrets: SecretsSettings = SecretsSettings()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Load and validate config once per process.

    Cached — tests that need a fresh load after changing env vars should
    call get_config.cache_clear() first.
    """
    return AppConfig()
