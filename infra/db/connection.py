"""Database connection layer.

Connection details come from `packages.config` (Module 02): host, port,
name and user from `DatabaseSettings`, and the password from
`SecretsProvider.get_secret("DATABASE_PASSWORD")`. That split is
deliberate — the password is never a config field, so it cannot end up in
a config object's repr, in a log line, or in a serialized settings dump.
The URL is assembled here, at the point of use, rather than being handed
around pre-assembled with the credential embedded.

Target is PostgreSQL 16 via psycopg 3.

## Two ways to be told where the database is

Hosting platforms hand out one connection string, not five discrete
fields: Railway, Fly, Heroku and most others inject `DATABASE_URL`. ARGUS
was built for the discrete form, so `build_database_url` now accepts
either — `DATABASE_URL` when present, the `ARGUS_DATABASE__*` fields
otherwise.

The connection string is resolved through `SecretsProvider`, not read from
`os.environ` here, because it carries the password inline. That keeps the
module-wide invariant intact: every credential in ARGUS comes from one
place, and there is no second path to audit.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url

from packages.config import AppConfig, SecretsProvider, get_config, get_secrets_provider
from packages.config.secrets import SecretNotFoundError

#: Key the database password is stored under in the secrets backend.
DATABASE_PASSWORD_SECRET = "DATABASE_PASSWORD"

#: Key a full connection string is stored under, when one is supplied.
#: `DATABASE_URL` rather than an `ARGUS_`-prefixed name because that is
#: what hosting platforms inject, and renaming it at the boundary would
#: mean every deployment needed a mapping step.
DATABASE_URL_SECRET = "DATABASE_URL"

#: The driver ARGUS connects with. A platform-supplied URL names the
#: dialect (`postgresql`) but not the driver, and SQLAlchemy would then
#: default to psycopg2, which is not installed.
DRIVER = "postgresql+psycopg"

#: Schemes a supplied URL may use. `postgres://` is the legacy form some
#: platforms still emit; SQLAlchemy rejects it outright, so it is
#: normalized rather than passed through.
ACCEPTED_SCHEMES = ("postgresql", "postgres")


def build_database_url(
    config: AppConfig | None = None,
    secrets: SecretsProvider | None = None,
) -> URL:
    """Assemble the connection URL from config plus the resolved password.

    A `DATABASE_URL` connection string wins when one is available; the
    discrete `ARGUS_DATABASE__*` fields are used otherwise. Local
    development and CI supply no `DATABASE_URL`, so they take the discrete
    path exactly as before.

    Returns a SQLAlchemy `URL`, not a string: `URL` masks the password in
    its own `repr`, so an accidentally logged URL object does not leak the
    credential. Call `.render_as_string(hide_password=False)` only where
    the real DSN is genuinely needed.
    """
    cfg = config or get_config()
    secret_provider = secrets or get_secrets_provider(cfg)

    supplied = _supplied_url(secret_provider)
    if supplied is not None:
        return supplied

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


def _supplied_url(secrets: SecretsProvider) -> URL | None:
    """A platform-supplied connection string, normalized, or None.

    Normalizing the driver matters more than it looks: a URL of
    `postgresql://…` is valid and SQLAlchemy accepts it, then reaches for
    psycopg2, which ARGUS does not install. The failure surfaces at
    connection time as a driver import error rather than as a
    configuration problem, which is a bad half-hour for whoever is on the
    other end of a first deployment.
    """
    try:
        raw = secrets.get_secret(DATABASE_URL_SECRET)
    except SecretNotFoundError:
        return None
    if not raw.strip():
        return None

    url = make_url(raw.strip())
    if url.drivername not in ACCEPTED_SCHEMES and not url.drivername.startswith("postgresql+"):
        raise ValueError(
            f"{DATABASE_URL_SECRET} names driver {url.drivername!r}; ARGUS targets "
            f"PostgreSQL 16 and connects with {DRIVER!r}. Refusing rather than "
            "connecting to something the schema's triggers and native enums will "
            "not work against."
        )
    return url.set(drivername=DRIVER)
