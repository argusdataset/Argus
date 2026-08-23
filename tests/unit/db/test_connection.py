"""The connection layer reads config from Module 02 and the password from secrets."""

from __future__ import annotations

import pytest

from infra.db.connection import (
    DATABASE_PASSWORD_SECRET,
    DATABASE_URL_SECRET,
    build_database_url,
)
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


# --------------------------------------------------------------------------
# The DATABASE_URL path (added during the deployment-readiness audit)
# --------------------------------------------------------------------------
#
# Every test above this line exercises the discrete `ARGUS_DATABASE__*`
# path with a stub carrying no `DATABASE_URL`, which is exactly how local
# development and CI run. They are the proof that the existing path is
# unchanged, and they were not modified to add this feature.

SUPPLIED_PASSWORD = "platform-injected-password"
SUPPLIED_URL = (
    f"postgresql://railway_user:{SUPPLIED_PASSWORD}@monorail.proxy.rlwy.net:41234/railway"
)


def _with_url(raw: str) -> StubSecretsProvider:
    return StubSecretsProvider(
        {DATABASE_URL_SECRET: raw, DATABASE_PASSWORD_SECRET: SECRET_PASSWORD}
    )


def test_a_supplied_connection_string_is_used_in_full(config: AppConfig):
    """Hosting platforms hand out one string, not five fields."""
    url = build_database_url(config, _with_url(SUPPLIED_URL))

    assert url.host == "monorail.proxy.rlwy.net"
    assert url.port == 41234
    assert url.database == "railway"
    assert url.username == "railway_user"
    assert url.password == SUPPLIED_PASSWORD


def test_a_supplied_url_overrides_the_discrete_settings(config: AppConfig):
    """The discrete fields are still configured here — `config` sets
    db.internal:6543 — and the supplied URL wins over all of them. A
    partial override would produce a connection to a host from one source
    and a database from another, which is the worst possible outcome."""
    url = build_database_url(config, _with_url(SUPPLIED_URL))

    assert url.host != config.database.host
    assert url.port != config.database.port
    assert url.database != config.database.name


@pytest.mark.parametrize(
    "scheme",
    ["postgresql", "postgres", "postgresql+psycopg"],
    ids=["modern", "legacy_heroku_style", "already_qualified"],
)
def test_every_accepted_scheme_normalizes_to_the_installed_driver(config, scheme):
    """The failure this prevents is nasty because it is late and misleading.

    `postgresql://…` is a perfectly valid URL that SQLAlchemy accepts, then
    reaches for psycopg2 — which ARGUS does not install. It surfaces at
    connection time as a driver import error rather than as a
    configuration problem. `postgres://` is worse: SQLAlchemy rejects it
    outright, and some platforms still emit it.
    """
    url = build_database_url(config, _with_url(f"{scheme}://u:p@h:5432/db"))

    assert url.drivername == "postgresql+psycopg"


def test_a_non_postgres_url_is_refused_rather_than_connected_to(config: AppConfig):
    """ARGUS's schema needs Postgres 16 specifically — plpgsql triggers,
    native enums, partial unique indexes. Connecting to MySQL would fail
    later and further from the cause."""
    with pytest.raises(ValueError, match="PostgreSQL 16"):
        build_database_url(config, _with_url("mysql://u:p@h:3306/db"))


@pytest.mark.parametrize("raw", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_connection_string_falls_back_rather_than_failing(config, raw):
    """An unset platform variable often arrives as an empty string. Taking
    that as "use this URL" would produce an unparseable DSN instead of the
    working discrete configuration sitting right there."""
    url = build_database_url(config, _with_url(raw))

    assert url.host == "db.internal"
    assert url.password == SECRET_PASSWORD


def test_the_supplied_password_still_does_not_leak_into_the_repr(config: AppConfig):
    """The credential arrives inline in the URL on this path, so the
    masking guarantee matters more here, not less."""
    url = build_database_url(config, _with_url(SUPPLIED_URL))

    assert SUPPLIED_PASSWORD not in repr(url)
    assert SUPPLIED_PASSWORD not in str(url)
    assert SUPPLIED_PASSWORD not in url.render_as_string()
    assert SUPPLIED_PASSWORD in url.render_as_string(hide_password=False)


def test_no_database_password_is_needed_when_a_url_is_supplied(config: AppConfig):
    """The URL carries its own credential. Requiring the discrete password
    as well would make every platform deployment set a variable it has no
    use for."""
    url = build_database_url(config, StubSecretsProvider({DATABASE_URL_SECRET: SUPPLIED_URL}))

    assert url.password == SUPPLIED_PASSWORD


def test_the_discrete_path_is_untouched_when_no_url_is_supplied(config: AppConfig):
    """Stated as its own test rather than left implicit in the tests above:
    with no DATABASE_URL present, the result is byte-identical to what the
    pre-audit code produced."""
    url = build_database_url(config, StubSecretsProvider())

    assert url.render_as_string(hide_password=False) == (
        f"postgresql+psycopg://argus_app:{SECRET_PASSWORD}@db.internal:6543/argus_prod"
    )
