"""G3: `get_config()` must load in the environment a deployment actually has.

Every deployed ARGUS service runs with `DATABASE_URL` and nothing else —
`.railway/railway.ts` sets exactly that, deliberately, because a platform
injects a connection string rather than four discrete fields. Until this
was fixed, `AppConfig.database` was a required group that a connection
string satisfied none of, so `get_config()` raised a `ValidationError` in
production and two modules had grown local workarounds around it.

The regression the whole entry turns on is the one at the bottom of this
file, and it runs in a **subprocess with a scrubbed environment and a
working directory containing no `.env`** — the documented repro verbatim.
In-process `monkeypatch` cannot prove this one: `AppConfig` declares
`env_file=".env"` and pydantic-settings reads that file from disk rather
than through `os.environ`, which is B1 in `KNOWN_ISSUES.md` and is still
open. A developer with a local `.env` would otherwise get a different
answer from CI, on precisely the test that is supposed to speak for
production.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.config.settings import (
    DEFAULT_POSTGRES_PORT,
    AppConfig,
    database_settings_from_url,
    get_config,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: A realistic platform-injected string: credentials inline, a non-default
#: port, and a query parameter of the kind managed Postgres adds.
PLATFORM_URL = "postgresql://argus_prod:sup3r%2Fsecret@db.internal:6543/argus?sslmode=require"


# --------------------------------------------------------------------------
# Parsing — pure, so no environment is involved at all
# --------------------------------------------------------------------------


def test_a_platform_connection_string_yields_every_database_field():
    derived = database_settings_from_url(PLATFORM_URL)

    assert derived == {
        "host": "db.internal",
        "port": 6543,
        "name": "argus",
        "user": "argus_prod",
    }


def test_percent_encoded_credentials_are_decoded():
    """A password containing `/` is percent-encoded by the platform; the
    username may be too, and a literal `%2F` in a username would be a
    connection failure nobody could read off the config."""
    derived = database_settings_from_url("postgresql://a%40b:pw@host/db")

    assert derived["user"] == "a@b"


def test_the_password_is_never_among_the_derived_fields():
    """The invariant `packages/config/settings.py`'s docstring rests on:
    this object is always safe to log."""
    derived = database_settings_from_url(PLATFORM_URL)

    assert "password" not in derived
    assert "sup3r/secret" not in str(derived)


def test_a_url_without_a_port_assumes_the_postgres_default():
    derived = database_settings_from_url("postgresql://argus:pw@db.internal/argus")

    assert derived["port"] == DEFAULT_POSTGRES_PORT


def test_the_legacy_postgres_scheme_is_accepted():
    """Some platforms still emit `postgres://`."""
    assert database_settings_from_url("postgres://argus:pw@db.internal/argus")["name"] == "argus"


def test_a_driver_qualified_scheme_is_accepted():
    """SQLAlchemy-style `postgresql+psycopg://` is what ARGUS itself writes."""
    assert database_settings_from_url("postgresql+psycopg://argus:pw@h/argus")["name"] == "argus"


def test_a_url_naming_no_database_leaves_that_field_to_fail_normally():
    """Partial rather than invented. A default database name would be a
    worse outcome than the crash it replaced — ARGUS would connect
    successfully to the wrong place."""
    derived = database_settings_from_url("postgresql://argus:pw@db.internal")

    assert "name" not in derived
    assert derived["host"] == "db.internal"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "mysql://argus:pw@db.internal/argus",
        "redis://cache:6379",
        "not-a-url-at-all",
        "postgresql://argus:pw@db.internal:notaport/argus",
    ],
)
def test_anything_that_is_not_a_usable_postgres_url_yields_nothing(raw: str):
    """`{}` rather than a raise: a bad `DATABASE_URL` should surface as the
    ordinary config error it is, not as a parse traceback from inside a
    validator."""
    assert database_settings_from_url(raw) == {}


# --------------------------------------------------------------------------
# AppConfig — the merge, in process
# --------------------------------------------------------------------------


def test_app_config_loads_from_a_connection_string_alone(monkeypatch):
    """The core of G3: no `ARGUS_DATABASE__*` anywhere, and it still loads."""
    monkeypatch.setenv("DATABASE_URL", PLATFORM_URL)

    config = AppConfig()

    assert config.database.host == "db.internal"
    assert config.database.port == 6543
    assert config.database.name == "argus"
    assert config.database.user == "argus_prod"


def test_explicit_settings_win_field_by_field(monkeypatch):
    """A deployment overriding one field must not have to restate the rest —
    pointing a second service at another database on the same server is
    the concrete case."""
    monkeypatch.setenv("DATABASE_URL", PLATFORM_URL)
    monkeypatch.setenv("ARGUS_DATABASE__NAME", "argus_reporting")

    config = AppConfig()

    assert config.database.name == "argus_reporting"
    # Everything not overridden still comes from the URL.
    assert config.database.host == "db.internal"
    assert config.database.port == 6543
    assert config.database.user == "argus_prod"


def test_the_discrete_path_is_untouched_when_no_url_is_set(required_db_env):
    """Local development and CI supply no `DATABASE_URL` and must behave
    exactly as they did before this existed."""
    config = AppConfig()

    assert config.database.port == 5432
    assert config.database.name == "argus_test"
    assert config.database.user == "argus_test_user"


def test_an_unusable_url_still_fails_the_way_it_always_did(monkeypatch):
    """No silent rescue: a `DATABASE_URL` pointing at MySQL leaves the
    original "Field required" error intact rather than half-filling the
    group and failing somewhere less obvious."""
    monkeypatch.setenv("DATABASE_URL", "mysql://argus:pw@db.internal/argus")

    with pytest.raises(ValidationError) as exc_info:
        AppConfig()

    assert "database" in str(exc_info.value)


def test_the_loaded_config_never_carries_the_password(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", PLATFORM_URL)

    config = AppConfig()

    assert "sup3r" not in repr(config)
    assert "sup3r" not in str(config.model_dump())


def test_get_config_works_too_not_just_direct_construction(monkeypatch):
    """`get_config()` is what every caller actually uses."""
    monkeypatch.setenv("DATABASE_URL", PLATFORM_URL)
    get_config.cache_clear()

    assert get_config().database.name == "argus"


# --------------------------------------------------------------------------
# The documented repro, in a real subprocess
# --------------------------------------------------------------------------


def _run_in_clean_environment(
    script: str, tmp_path: Path, **env: str
) -> subprocess.CompletedProcess:
    """Run `script` the way production runs: a scrubbed environment, and a
    working directory with no `.env` in it.

    `env -i` in the documented repro, expressed portably. `cwd=tmp_path`
    is the half that defeats B1: `env_file=".env"` is a *relative* path,
    so running from a directory that has none makes the test give the same
    answer on a developer machine as in CI.
    """
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT), **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_known_issues_repro_no_longer_raises(tmp_path):
    """G3's repro, verbatim, as a regression test.

    Previously:

        ValidationError: 1 validation error for AppConfig
        database
          Field required
    """
    script = (
        "from packages.config.settings import get_config\n"
        "config = get_config()\n"
        "print(config.database.name, config.database.user, config.database.port)\n"
    )
    result = _run_in_clean_environment(
        script,
        tmp_path,
        DATABASE_URL="postgresql://argus:pw@db.internal:5432/argus_prod",
        ARGUS_ENV="production",
    )

    assert result.returncode == 0, result.stderr
    assert "ValidationError" not in result.stderr
    assert result.stdout.strip() == "argus_prod argus 5432"


def test_the_fmp_client_constructs_in_that_same_environment(tmp_path):
    """The call site G3 names first. `FMP_API_KEY` is supplied because
    Railway supplies it (`SECRET_VARIABLES` in `infra/deploy/railway.py`);
    what is under test is that nothing raises a *config* error on the way.
    """
    script = (
        "from data.provider_adapters.fmp.client import FmpClient\n"
        "FmpClient()\n"
        "print('constructed')\n"
    )
    result = _run_in_clean_environment(
        script,
        tmp_path,
        DATABASE_URL="postgresql://argus:pw@db.internal:5432/argus_prod",
        FMP_API_KEY="test-key",
        ARGUS_ENV="production",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "constructed"


def test_the_ingestion_orchestrators_config_step_survives_it(tmp_path):
    """The second call site G3 names: `run_daily_ingestion`'s
    `app_config or get_config()`. Called directly rather than through the
    orchestrator so the assertion is about configuration and not about a
    database that does not exist in this environment."""
    script = (
        "import core.ingestion.orchestrator as orchestrator\n"
        "from packages.config.settings import get_config\n"
        "config = get_config()\n"
        "assert orchestrator.run_daily_ingestion is not None\n"
        "print(config.providers.fmp_requests_per_minute)\n"
    )
    result = _run_in_clean_environment(
        script,
        tmp_path,
        DATABASE_URL="postgresql://argus:pw@db.internal:5432/argus_prod",
        ARGUS_ENV="production",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "300"


def test_a_deployment_with_no_database_url_at_all_still_reports_it_clearly(tmp_path):
    """The failure that should still happen, and still be readable: nothing
    here rescues a process that genuinely has no database configuration."""
    script = (
        "from packages.config.settings import get_config\n"
        "try:\n"
        "    get_config()\n"
        "except Exception as error:\n"
        "    print(type(error).__name__)\n"
        "    print('database' in str(error))\n"
    )
    result = _run_in_clean_environment(script, tmp_path, ARGUS_ENV="production")

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["ValidationError", "True"]
