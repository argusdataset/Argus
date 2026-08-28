"""The committed Railway configuration, checked against its generator.

A generated file that is committed has one failure mode: somebody edits
the generator, forgets to regenerate, and the platform keeps deploying
last week's command. The point of committing it at all is that it is
reviewable in a pull request, and it is only reviewable if it is true.
"""

from __future__ import annotations

import json

import pytest

from infra.deploy.processes import PRE_DEPLOY_COMMAND, PROCESSES
from infra.deploy.railway import CONFIG_DIR, config_for, render_all

SECRET_SHAPED = ("password", "secret", "token", "api_key", "apikey", "credential")


def test_there_is_one_config_file_per_process():
    committed = {path.name for path in CONFIG_DIR.glob("*.json")}
    assert committed == set(render_all())


@pytest.mark.parametrize("filename", sorted(render_all()))
def test_the_committed_file_matches_what_the_generator_produces_now(filename: str):
    """Regenerate with `python -m infra.deploy.railway` when this fails."""
    assert (CONFIG_DIR / filename).read_text() == render_all()[filename]


@pytest.mark.parametrize("name", sorted(PROCESSES))
def test_the_start_command_is_the_process_definition_verbatim(name: str):
    config = json.loads((CONFIG_DIR / f"{name}.json").read_text())
    assert config["deploy"]["startCommand"] == PROCESSES[name].command()


def test_exactly_one_service_owns_the_migration():
    """Seven services running the same migration would race.

    Alembic's version table is not a lock, so concurrent `upgrade head`
    calls are a genuine race rather than a redundant one.
    """
    owners = [
        name
        for name in PROCESSES
        if json.loads((CONFIG_DIR / f"{name}.json").read_text())["deploy"].get("preDeployCommand")
    ]
    assert owners == ["identity"]


def test_the_migration_runs_as_a_pre_deploy_command():
    """Which is what makes it run *before* traffic reaches the new code.

    A migration invoked at application startup would run after the
    container is already accepting traffic — and once per container.
    """
    deploy = json.loads((CONFIG_DIR / "identity.json").read_text())["deploy"]
    assert deploy["preDeployCommand"] == PRE_DEPLOY_COMMAND


@pytest.mark.parametrize("name", sorted(PROCESSES))
def test_only_web_services_are_health_checked(name: str):
    deploy = json.loads((CONFIG_DIR / f"{name}.json").read_text())["deploy"]
    if PROCESSES[name].is_web:
        assert deploy["healthcheckPath"] == "/health/live"
        assert deploy["healthcheckTimeout"] > 0
    else:
        assert "healthcheckPath" not in deploy


@pytest.mark.parametrize("name", sorted(PROCESSES))
def test_a_failed_container_stops_rather_than_restarting_forever(name: str):
    """A crash loop that looks like activity hides a container that cannot start."""
    deploy = json.loads((CONFIG_DIR / f"{name}.json").read_text())["deploy"]
    assert deploy["restartPolicyType"] == "ON_FAILURE"
    assert deploy["restartPolicyMaxRetries"] > 0


@pytest.mark.parametrize("name", sorted(PROCESSES))
def test_no_config_declares_an_environment_variable(name: str):
    """Secrets reach a deployed process through `SecretsProvider` and nowhere else.

    A committed config listing variables would be a second place they
    live and a second place they leak — and this file is in git.
    """
    config = json.loads((CONFIG_DIR / f"{name}.json").read_text())
    assert "variables" not in config
    assert "environment" not in config
    assert "env" not in config.get("deploy", {})

    body = json.dumps(config).lower()
    for word in SECRET_SHAPED:
        assert word not in body, f"{name}.json mentions {word!r}"


def test_config_for_reads_the_process_rather_than_a_name():
    """The generator takes a definition, so a test can pass a made-up one."""
    scanner = config_for(PROCESSES["scanner"])
    assert scanner["deploy"]["cronSchedule"] == PROCESSES["scanner"].schedule
    assert "healthcheckPath" not in scanner["deploy"]
