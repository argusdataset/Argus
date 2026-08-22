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
    """External data provider settings, used by Module 04's FMP adapter.

    No API key field — the FMP credential is a secret, resolved via
    SecretsProvider, never stored on this object.

    Rate limits are configuration rather than constants because they are a
    property of the *subscription*, not of the API: FMP's published
    per-minute limits differ by plan (Starter 300, Premium 750, Ultimate
    3000), and the bulk-download endpoints carry their own much stricter
    limit. Changing plan must not require a code change.
    """

    fmp_base_url: str = "https://financialmodelingprep.com"
    fmp_request_timeout_seconds: int = 30

    # Standard endpoints. Defaults to the Starter plan's published limit,
    # the most conservative paid tier.
    fmp_requests_per_minute: int = 300

    # Bulk CSV downloads are throttled far harder than standard endpoints
    # (FMP documents roughly one download per 10s, and one per minute for
    # profile/ETF-holder bulk), so they get their own budget.
    fmp_bulk_requests_per_minute: int = 6

    # Ceiling on simultaneous in-flight requests. The rate limiter governs
    # throughput; this bounds how much is outstanding at once.
    fmp_max_concurrency: int = 8

    # Retries for transient failures (429 and 5xx) before giving up.
    fmp_max_retries: int = 5
    fmp_backoff_base_seconds: float = 1.0
    fmp_backoff_max_seconds: float = 60.0

    # Filesystem response cache. Historical bars do not change, so a
    # re-run should not re-fetch them.
    fmp_cache_enabled: bool = True
    fmp_cache_dir: str = ".cache/fmp"

    # Where resumable job checkpoints are written.
    fmp_checkpoint_dir: str = ".cache/fmp/checkpoints"


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
