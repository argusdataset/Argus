"""Railway service configuration, generated from `processes.py` rather than
maintained beside it.

Railway reads a JSON config per service, and a project with seven
services means seven files. Hand-maintained, they are six copies of facts that live
in `PROCESSES` — the start command, the health path, the pre-deploy step
— and the failure mode is not that they break loudly but that one of
them quietly keeps an old command after the code moved on.

So they are generated, committed, and a test asserts the committed files
match what this module would generate now. That gives the reviewable
artefact a platform needs (a file in the repo, diffable in a pull
request) without the duplication a hand-written one carries.

## What is in the config, and what deliberately is not

**In**: builder, dockerfile path, start command, pre-deploy command,
health check path and timeout, restart policy.

**Not in**: environment variables, and especially not secrets. Railway
injects those from its own store, which is what the deployment audit's
`EnvironmentSecretsProvider` work was for — `SecretsProvider` reads them
from the process environment and no ARGUS code reads a credential from
anywhere else. A `railway.json` listing variables would be a second
place they live, and a committed one would be a second place they leak.

## Health checks and the restart policy

Railway will not route traffic to a container whose health check does not
pass, which is what makes the check a deployment-safety mechanism rather
than a monitoring one. It polls `/health/live`, which answers up/down and
nothing else and which Module 24 exempted from the rate limiter precisely
so a burst cannot make a healthy instance look dead.

Only the health service has that route of its own. The other four answer
it through `infra/deploy/liveness.py`, wrapped on at composition time —
without which Railway would poll a 404 and never route traffic to any of
them. That is a deployment concern rather than an application one, which
is why it lives at this layer.

`ON_FAILURE` with a bounded retry count rather than `ALWAYS`: a container
that cannot start — a bad migration, a `ProductionMisconfigured` — should
stop and stay visibly stopped, not restart forever in a loop that looks
like activity. Ten attempts is enough to ride out a database that is
briefly unreachable and few enough that a genuine failure surfaces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from infra.deploy.processes import PRE_DEPLOY_COMMAND, PROCESSES, ProcessDefinition

__all__ = ["CONFIG_DIR", "config_for", "render_all", "write_all"]

CONFIG_DIR = Path(__file__).resolve().parent / "railway"

#: Seconds Railway waits for the health check to pass before calling the
#: deploy failed. Generous, because a cold container has to import pandas
#: and open a connection pool before it can answer anything, and a
#: too-tight timeout turns a slow start into a failed deploy.
HEALTHCHECK_TIMEOUT_SECONDS = 120

RESTART_MAX_RETRIES = 10


def config_for(process: ProcessDefinition) -> dict[str, Any]:
    """One service's Railway configuration."""
    config: dict[str, Any] = {
        "$schema": "https://railway.com/railway.schema.json",
        "build": {
            "builder": "DOCKERFILE",
            "dockerfilePath": "Dockerfile",
        },
        "deploy": {
            "startCommand": process.command(),
            "restartPolicyType": "ON_FAILURE",
            "restartPolicyMaxRetries": RESTART_MAX_RETRIES,
        },
    }

    if process.is_web:
        # A cron service has nothing to poll between firings, so the
        # health check belongs to the web services alone.
        config["deploy"]["healthcheckPath"] = process.health_path
        config["deploy"]["healthcheckTimeout"] = HEALTHCHECK_TIMEOUT_SECONDS

    if process.name == "identity":
        # Migrations run before traffic routes to the new container, and
        # a non-zero exit abandons the deploy. On exactly one service:
        # seven running the same migration concurrently would race, and
        # Alembic's version table is not a lock.
        #
        # Identity rather than an arbitrary pick: it is the service whose
        # schema every other service's authentication depends on, so if
        # its migration fails, the correct outcome is that nothing else
        # deploys either.
        config["deploy"]["preDeployCommand"] = PRE_DEPLOY_COMMAND

    if process.schedule:
        config["deploy"]["cronSchedule"] = process.schedule

    return config


def render_all() -> dict[str, str]:
    """Every service's config, as `{filename: json text}`."""
    return {
        f"{name}.json": json.dumps(config_for(process), indent=2) + "\n"
        for name, process in PROCESSES.items()
    }


def write_all(directory: Path | None = None) -> list[Path]:
    """Write the generated configs. Returns the paths written."""
    target = directory or CONFIG_DIR
    target.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for filename, body in render_all().items():
        path = target / filename
        path.write_text(body)
        written.append(path)
    return written


if __name__ == "__main__":  # pragma: no cover - regeneration by hand
    for path in write_all():
        print(path)
