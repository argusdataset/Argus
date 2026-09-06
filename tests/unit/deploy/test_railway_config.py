"""The committed Railway IaC file, checked against its generator.

A generated file that is committed has one failure mode: somebody edits the
generator, forgets to regenerate, and the platform keeps deploying last
week's command. The point of committing it is that it is reviewable in a
pull request, and it is only reviewable if it is true.

The previous version of this module tested seven `railway.json` files. Those
are gone: Railway deprecated Config as Code, and an audit of the live project
found none of them had ever been read — all three deployed services were
building with Railpack. These tests are deliberately harder to satisfy
vacuously than those were, because those passed for months while describing
a deployment that did not exist.
"""

from __future__ import annotations

import re

import pytest

from infra.deploy.processes import PRE_DEPLOY_COMMAND, PROCESSES
from infra.deploy.railway import (
    DOCKERFILE_PATH,
    FMP_TUNING_VARIABLES,
    HEALTHCHECK_TIMEOUT_SECONDS,
    IAC_PATH,
    POSTGRES_SERVICE,
    PROVIDER_TUNING,
    RESTART_MAX_RETRIES,
    RESTART_POLICY,
    SERVICE_NAMES,
    UNSUPPORTED_BY_IAC,
    dashboard_settings,
    render,
)

SECRET_SHAPED = ("password", "secret", "token", "api_key", "apikey", "credential")


@pytest.fixture(scope="module")
def committed() -> str:
    return IAC_PATH.read_text()


def test_the_committed_file_matches_what_the_generator_produces_now(committed: str):
    """Regenerate with `python -m infra.deploy.railway` when this fails."""
    assert committed == render()


def test_there_is_exactly_one_authoring_file():
    """Railway refuses to choose between two. `.railway/` holds ts, py or go."""
    siblings = sorted(p.name for p in IAC_PATH.parent.glob("railway.*"))
    assert siblings == ["railway.ts"]


@pytest.mark.parametrize("name", sorted(PROCESSES))
def test_the_start_command_is_the_process_definition_verbatim(name: str, committed: str):
    assert PROCESSES[name].command() in committed


@pytest.mark.parametrize("name", sorted(PROCESSES))
def test_every_process_appears_under_its_real_railway_name(name: str, committed: str):
    """Renaming here does not rename on Railway. It destroys and recreates.

    A service recreated under a new name loses its variables and its
    deployment history, so the names in SERVICE_NAMES track the dashboard
    rather than the other way round.
    """
    assert f'service("{SERVICE_NAMES[name]}"' in committed


def test_the_database_is_declared_so_apply_cannot_offer_to_delete_it(committed: str):
    """IaC reads omission as deletion. Postgres is the one resource whose
    deletion is not recoverable from this repository."""
    assert f'postgres("{POSTGRES_SERVICE}")' in committed


def test_every_resource_is_reachable_from_the_project(committed: str):
    """A service defined but left out of `resources` is a service apply
    would offer to destroy — the failure this test exists to catch."""
    resources = re.search(r"resources: \[([^\]]*)\]", committed)
    assert resources is not None
    listed = {item.strip() for item in resources.group(1).split(",")}
    expected = {"db"} | {name if name != "identity" else "identityService" for name in PROCESSES}
    assert listed == expected


@pytest.mark.parametrize("name", sorted(PROCESSES))
def test_only_web_services_are_health_checked(name: str, committed: str):
    block = _service_block(committed, SERVICE_NAMES[name])
    if PROCESSES[name].is_web:
        assert 'healthcheck: "/health/live"' in block
        assert f"healthcheckTimeout: {HEALTHCHECK_TIMEOUT_SECONDS}" in block
    else:
        # A cron service has no path to poll and no process running between
        # firings. A probe on one answers a question nobody is asking.
        assert "healthcheck" not in block


def test_no_secret_value_is_written_into_the_file(committed: str):
    """Secrets reach a deployed process through `SecretsProvider`. A
    committed value would be a second place they live and a second place
    they leak — and this file is in git."""
    for line in committed.splitlines():
        if line.strip().startswith("//"):
            continue
        lowered = line.lower()
        if any(word in lowered for word in SECRET_SHAPED):
            assert "preserve()" in line, f"secret-shaped line carries a value: {line!r}"


def test_the_environment_decides_the_profile_rather_than_a_per_service_literal(
    committed: str,
):
    """Two of the three live services ran the development profile in
    production because ARGUS_ENV was written down per service and one copy
    was wrong. Deriving it from the Railway environment removes the copies."""
    assert 'ARGUS_ENV: prod ? "production" : "staging"' in committed
    assert 'ARGUS_ENV: "development"' not in committed


def test_the_scanner_is_told_its_universe(committed: str):
    """scanner.py raises ScannerNotReady rather than guessing one."""
    block = _service_block(committed, SERVICE_NAMES["scanner"])
    assert "ARGUS_UNIVERSE_VERSION" in block


# --- the half IaC cannot carry ------------------------------------------


def test_the_iac_gap_is_recorded_rather_than_remembered():
    """Four settings have no field in the DSL. Naming them in data is what
    keeps `railway config apply` from looking like a complete deployment."""
    settings = {setting for setting, _, _ in UNSUPPORTED_BY_IAC}
    assert settings == {
        "dockerfile",
        "preDeployCommand",
        "cronSchedule",
        "restartPolicy",
    }
    for _, processes, reason in UNSUPPORTED_BY_IAC:
        assert processes, "a gap that affects no process is not a gap"
        assert reason.strip(), "a gap without its consequence is a shrug"


@pytest.mark.parametrize("name", sorted(PROCESSES))
def test_every_service_builds_from_the_dockerfile(name: str):
    """Railpack builds a different image: no non-root user, and no
    postgresql-client, which is what backup.py shells out to."""
    settings = dashboard_settings(name)
    assert settings["builder"] == "DOCKERFILE"
    assert settings["dockerfilePath"] == DOCKERFILE_PATH


@pytest.mark.parametrize("name", sorted(PROCESSES))
def test_a_failed_container_stops_rather_than_restarting_forever(name: str):
    settings = dashboard_settings(name)
    assert settings["restartPolicyType"] == RESTART_POLICY
    assert settings["restartPolicyMaxRetries"] == RESTART_MAX_RETRIES > 0


def test_exactly_one_service_owns_the_migration():
    """Seven services running the same migration would race. Alembic's
    version table is not a lock."""
    owners = [name for name in PROCESSES if "preDeployCommand" in dashboard_settings(name)]
    assert owners == ["identity"]


def test_the_migration_runs_as_a_pre_deploy_command():
    """Which is what makes it run *before* traffic reaches the new code.

    Run from the start command instead — as the live deployment did until
    this was fixed — a failed migration is a crash loop rather than an
    abandoned deploy, and it runs once per container rather than once.
    """
    assert dashboard_settings("identity")["preDeployCommand"] == [PRE_DEPLOY_COMMAND]


@pytest.mark.parametrize("name", sorted(PROCESSES))
def test_only_cron_processes_carry_a_schedule(name: str):
    settings = dashboard_settings(name)
    if PROCESSES[name].schedule:
        assert settings["cronSchedule"] == PROCESSES[name].schedule
    else:
        assert "cronSchedule" not in settings


def _service_block(committed: str, service_name: str) -> str:
    start = committed.index(f'service("{service_name}"')
    end = committed.index("});", start)
    return committed[start:end]


# --------------------------------------------------------------------------
# FMP plan tuning
#
# The generated file is the whole environment — "omit means delete" — so a
# rate raised by hand in the Railway panel was removed by the next apply
# and the code fell back to `fmp_requests_per_minute = 300`, the Starter
# limit. An Ultimate subscription would have run at a tenth of its
# throughput, and `strategy.py`'s bulk path (which needs >= 3000) would
# never have enabled, with nothing saying so.
#
# The values are deliberately not chosen here: which numbers are correct
# depends on the plan actually paid for. What is fixed is that the
# variables are *named*, so a value set in the panel survives.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PROVIDER_TUNING))
def test_a_fetching_service_can_carry_its_fmp_tuning(committed: str, name: str):
    """Named in the file, so `preserve()` keeps whatever Railway holds."""
    block = _service_block_for(committed, name)

    for variable in FMP_TUNING_VARIABLES:
        assert f"{variable}: preserve()" in block


def test_the_tuning_variables_are_the_names_the_settings_actually_read():
    """A variable nobody reads is worse than no variable.

    `ProvidersSettings` is nested under `AppConfig`, so the names carry
    the `ARGUS_` prefix and the `__` delimiter — get either wrong and the
    value is accepted by Railway, ignored by the process, and the
    difference is invisible until someone measures the request rate.
    """
    from packages.config.settings import ProvidersSettings

    fields = set(ProvidersSettings.model_fields)
    for variable in FMP_TUNING_VARIABLES:
        assert variable.startswith("ARGUS_PROVIDERS__")
        assert variable.removeprefix("ARGUS_PROVIDERS__").lower() in fields


def test_the_defaults_stay_conservative():
    """This change opens a door; it must not walk through it.

    Running ten times too fast against a plan that does not allow it is a
    worse failure than running slowly, so the shipped defaults stay at
    the most conservative paid tier and the operator raises them
    deliberately.
    """
    from packages.config.settings import ProvidersSettings

    settings = ProvidersSettings()
    assert settings.fmp_requests_per_minute == 300
    assert settings.fmp_max_concurrency == 8


def test_no_service_that_does_not_fetch_carries_provider_tuning(committed: str):
    """Scoped to the two processes that actually call FMP.

    `news_signals` and `ownership_signals` read tables ingestion already
    filled; a rate limit there would be a variable with no effect and a
    reader wondering what it does.
    """
    for name in sorted(set(PROCESSES) - set(PROVIDER_TUNING)):
        block = _service_block_for(committed, name)
        for variable in FMP_TUNING_VARIABLES:
            assert variable not in block


def _service_block_for(committed: str, name: str) -> str:
    """One service's generated block, from its start command to its close.

    Sliced from the rendered text rather than re-rendered, so these
    assertions are about the file that is committed and applied.
    """
    identifier = name if name != "identity" else "identityService"
    start = committed.index(f"const {identifier} = service(")
    return committed[start : committed.index("});", start)]
