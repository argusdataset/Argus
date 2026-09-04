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

## `DATABASE_URL` is a first-class source for the `database` group

Hosting platforms hand out one connection string, not four discrete
fields: Railway, Fly, Heroku and the rest inject `DATABASE_URL`. Until
this was addressed, `AppConfig.database` was a required group that a
connection string satisfied none of, so `get_config()` raised a
`ValidationError` in every deployed process — recorded as G3 in
`docs/architecture/KNOWN_ISSUES.md`, and reproduced there as:

    env -i DATABASE_URL=... ARGUS_ENV=production python -c "
        from data.provider_adapters.fmp.client import FmpClient; FmpClient()"
    ValidationError: 1 validation error for AppConfig
    database
      Field required

Two modules worked around it locally — `infra/db/connection.py` and, via
`bootstrap_secrets_provider()`, `services/telegram/app.py` — but a
workaround per call site is not a fix: any *new* code path calling
`get_config()` walked into the same wall, and Module 26's ingestion cron
was about to.

So `host`, `port`, `name` and `user` are now derived from `DATABASE_URL`
when it is set, field by field, and **explicit `ARGUS_DATABASE__*` values
still win** over anything the URL implies. The password embedded in the
connection string is deliberately *not* read: `DatabaseSettings` has no
password field, and the "always safe to log" invariant above depends on
that staying true. The credential continues to be resolved at connection
time through `SecretsProvider`, exactly as before.

Read from `os.environ` rather than from a `.env` file, which is the one
place this diverges from `ChainedSecretsProvider`'s ".env first" order.
Two reasons: a developer running against a local Postgres already has the
discrete `ARGUS_DATABASE__*` path, and reading `.env` here would widen the
blast radius of B1 (a local `.env` leaking past the test suite's env
isolation) to a field that decides whether config loads at all.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.config.environment import Environment
from packages.config.execution import ExecutionMode

#: The variable hosting platforms inject. Not `ARGUS_`-prefixed because
#: that is not what platforms emit, and renaming it at the boundary would
#: mean every deployment needed a mapping step.
DATABASE_URL_ENV_VAR = "DATABASE_URL"

#: Assumed when a connection string omits the port. PostgreSQL's own
#: default, and the only value a URL without one can mean.
DEFAULT_POSTGRES_PORT = 5432

#: Schemes a connection string may use. `postgres://` is the legacy form
#: some platforms still emit.
_POSTGRES_SCHEMES = ("postgresql", "postgres")


def database_settings_from_url(raw: str) -> dict[str, Any]:
    """The `database` fields a connection string implies, or `{}`.

    Deliberately partial and deliberately forgiving. A URL that names no
    database or no user yields a dict missing those keys, so the ordinary
    "Field required" error still surfaces for them — inventing a default
    database name would be worse than the crash it replaced.

    Parsed with `urllib.parse` rather than SQLAlchemy's `make_url`, which
    would make `packages/config` — the lowest layer in the project —
    depend on the database toolkit. The standard library is enough:
    percent-encoded credentials are decoded, query parameters
    (`?sslmode=require`) are ignored, and a malformed URL returns `{}`
    rather than raising, because a bad `DATABASE_URL` should surface as
    the config error it is and not as a parse traceback from inside a
    validator.
    """
    try:
        parsed = urlsplit(raw.strip())
    except ValueError:
        return {}

    scheme = parsed.scheme.split("+", 1)[0]
    if scheme not in _POSTGRES_SCHEMES:
        return {}

    derived: dict[str, Any] = {}
    try:
        port = parsed.port
    except ValueError:  # non-numeric port in the URL
        return {}

    if parsed.hostname:
        derived["host"] = parsed.hostname
    derived["port"] = port or DEFAULT_POSTGRES_PORT
    name = unquote(parsed.path.lstrip("/"))
    if name:
        derived["name"] = name
    if parsed.username:
        derived["user"] = unquote(parsed.username)
    return derived


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

    @model_validator(mode="before")
    @classmethod
    def _fill_database_from_connection_string(cls, values: Any) -> Any:
        """Derive the `database` group from `DATABASE_URL`, if it is set.

        `mode="before"` because `database` is a required field: by the
        time an "after" validator could run, validation has already
        failed with the very error this exists to prevent.

        Merged field by field with explicit settings winning, rather than
        all-or-nothing. A deployment that supplies `DATABASE_URL` and
        overrides only `ARGUS_DATABASE__NAME` — pointing one service at a
        second database on the same server — gets exactly that, instead
        of having to restate every field to change one.

        The URL is not stored anywhere on the returned object. Only the
        four non-secret fields are taken from it; the password stays in
        the environment and is resolved at connection time through
        `SecretsProvider`, so this object remains safe to log.
        """
        if not isinstance(values, dict):
            return values

        raw = os.environ.get(DATABASE_URL_ENV_VAR, "")
        if not raw.strip():
            return values

        derived = database_settings_from_url(raw)
        if not derived:
            return values

        supplied = values.get("database")
        if isinstance(supplied, DatabaseSettings):
            supplied = supplied.model_dump()
        if not isinstance(supplied, dict):
            supplied = {}

        merged = {**derived, **{key: value for key, value in supplied.items() if value is not None}}
        return {**values, "database": merged}


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Load and validate config once per process.

    Cached — tests that need a fresh load after changing env vars should
    call get_config.cache_clear() first.
    """
    return AppConfig()
