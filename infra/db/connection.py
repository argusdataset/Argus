"""Database connection layer.

Connection details come from `packages.config` (Module 02): host, port,
name and user from `DatabaseSettings`, and the password from
`SecretsProvider.get_secret("DATABASE_PASSWORD")`. That split is
deliberate — the password is never a config field, so it cannot end up in
a config object's repr, in a log line, or in a serialized settings dump.
The URL is assembled here, at the point of use, rather than being handed
around pre-assembled with the credential embedded.

Target is PostgreSQL via psycopg 3.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL

from packages.config import AppConfig, SecretsProvider, get_config, get_secrets_provider

#: Key the database password is stored under in the secrets backend.
DATABASE_PASSWORD_SECRET = "DATABASE_PASSWORD"


def build_database_url(
    config: AppConfig | None = None,
    secrets: SecretsProvider | None = None,
) -> URL:
    """Assemble the connection URL from config plus the resolved password.

    Returns a SQLAlchemy `URL`, not a string: `URL` masks the password in
    its own `repr`, so an accidentally logged URL object does not leak the
    credential. Call `.render_as_string(hide_password=False)` only where
    the real DSN is genuinely needed.
    """
    cfg = config or get_config()
    secret_provider = secrets or get_secrets_provider(cfg)
    return URL.create(
        drivername="postgresql+psycopg",
        username=cfg.database.user,
        password=secret_provider.get_secret(DATABASE_PASSWORD_SECRET),
        host=cfg.database.host,
        port=cfg.database.port,
        database=cfg.database.name,
    )


def create_db_engine(
    config: AppConfig | None = None,
    secrets: SecretsProvider | None = None,
    **engine_kwargs: object,
) -> Engine:
    """Create a SQLAlchemy Engine for the configured database.

    No engine is created at import time and none is cached here: the
    caller owns the engine's lifecycle. Modules 04+ will decide their own
    pooling needs, and the live scanner's long batch runs have very
    different requirements from an API process.
    """
    return create_engine(build_database_url(config, secrets), **engine_kwargs)
