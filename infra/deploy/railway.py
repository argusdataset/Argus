"""Railway's Infrastructure as Code file, generated from `processes.py`.

## Why this module was rewritten

It used to generate seven `railway.json` files under `infra/deploy/railway/`.
Railway has **deprecated Config as Code**: `railway.json` and `railway.toml`
are read only for services that already used them, new services cannot opt
in, and existing files stop being read on **2026-12-01**. The replacement is
Infrastructure as Code — one `.railway/railway.ts` describing the whole
environment, evaluated by the Railway CLI (`railway config plan` / `apply`).

The old files were worse than deprecated: they were never in effect. A live
audit of the deployed project found all three running services building with
Railpack, not the Dockerfile, because nothing on Railway ever pointed at
`infra/deploy/railway/*.json`. A committed configuration that the platform
does not read is not configuration; it is a comment that looks authoritative.

The generation idea survives that. `PROCESSES` is still written once and read
several times, and a test still asserts the committed file matches what this
module produces now. Only the target format changed.

## What IaC cannot express — read this before trusting the file

The documented TypeScript DSL covers `source`, `build`, `start`,
`healthcheck`, `healthcheckTimeout`, `replicas`, `env`, `domains`,
`volumeMounts`, databases, buckets and groups. It has **no** field for four
settings ARGUS depends on:

| Setting                     | Needed by            | Where it has to live instead |
| --------------------------- | -------------------- | ---------------------------- |
| Dockerfile builder + path   | every service        | Railway service settings     |
| `preDeployCommand`          | `identity`           | Railway service settings     |
| `cronSchedule`              | `ingestion`, `scanner`, `telegram_dispatch`, `retention` | Railway service settings |
| Restart policy + retries    | every service        | Railway service settings     |

`UNSUPPORTED_BY_IAC` below is that table as data, and `dashboard_settings()`
renders the exact values an operator or an API call must set per service. The
gap is enumerated in code rather than described in prose so that it can be
asserted on, printed, and diffed — the same reason the start commands are.

Without those four, the generated file alone is not a complete deployment.
It is the half a platform will accept declaratively; the other half is
`dashboard_settings()`.

## Safety: omit means delete

IaC treats the file as the whole environment. A service that exists on
Railway and is missing from the file is a service `railway config apply`
offers to destroy. So `SERVICE_NAMES` uses the names the services actually
carry on Railway today — including `"Identity "`, whose trailing space is a
typo in the dashboard rather than in this file — and the managed Postgres is
declared alongside the ten processes.

Never apply this file blind. `railway config plan` first, read every line
marked destructive, and treat any unexpected delete as a bug in this
generator rather than an instruction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infra.deploy.processes import PRE_DEPLOY_COMMAND, PROCESSES, ProcessDefinition

__all__ = [
    "DOCKERFILE_PATH",
    "GITHUB_BRANCH",
    "GITHUB_REPO",
    "HEALTHCHECK_TIMEOUT_SECONDS",
    "IAC_PATH",
    "POSTGRES_SERVICE",
    "RAILWAY_PROJECT",
    "RESTART_MAX_RETRIES",
    "RESTART_POLICY",
    "SERVICE_NAMES",
    "UNSUPPORTED_BY_IAC",
    "dashboard_settings",
    "render",
    "write",
]

#: Repository root, four levels up from this file.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The one authoring file. Railway requires exactly one under `.railway/`.
IAC_PATH = REPO_ROOT / ".railway" / "railway.ts"

RAILWAY_PROJECT = "passionate-unity"
GITHUB_REPO = "argusdataset/Argus"

#: The branch Railway deploys. Changing it here changes nothing on its own —
#: the source is also set per service in the dashboard, and IaC's `github()`
#: source is what reconciles the two on the next apply.
GITHUB_BRANCH = "main"

DOCKERFILE_PATH = "Dockerfile"

#: Seconds Railway waits for the health check before calling the deploy
#: failed. Generous, because a cold container imports pandas and opens a
#: connection pool before it can answer anything.
HEALTHCHECK_TIMEOUT_SECONDS = 120

#: `ON_FAILURE` rather than `ALWAYS`: a container that cannot start should
#: stop and stay visibly stopped, not restart forever in a loop that looks
#: like activity.
RESTART_POLICY = "ON_FAILURE"
RESTART_MAX_RETRIES = 10

#: The managed Postgres, by its Railway name. Declared so that `apply` does
#: not read its absence from the file as an instruction to delete it.
POSTGRES_SERVICE = "Postgres"

#: Process name -> the service name as it exists on Railway today.
#:
#: These are deliberately not tidied. Renaming a service in this file does
#: not rename it on Railway; it destroys one service and creates another,
#: taking its variables and its deployment history with it. Rename in the
#: dashboard first, then change the value here.
#:
#: Known defects to fix in the dashboard, in this order:
#:   - "Argus" serves the Terminal. The name says nothing about that.
#:   - "Identity " carries a trailing space.
#:   - "ingestion" does not exist on Railway yet. Unlike the six that
#:     also do not exist, this one is not optional: the scanner cannot
#:     produce anything until it runs. See infra/deploy/README.md.
SERVICE_NAMES: dict[str, str] = {
    "terminal": "Argus",
    "public_stats": "Public_stats",
    "intelligence": "intelligence",
    "identity": "Identity ",
    "telegram": "telegram",
    "health": "health",
    "ingestion": "ingestion",
    "scanner": "scanner",
    "telegram_dispatch": "telegram_dispatch",
    "retention": "retention",
}

#: Variables each process needs that are secret. Named here, never valued:
#: the file renders them as `preserve()`, which means "keep whatever Railway
#: already holds". A secret has to exist in Railway before the first apply
#: that mentions it — `preserve()` cannot invent one.
SECRET_VARIABLES: dict[str, tuple[str, ...]] = {
    "terminal": ("FMP_API_KEY",),
    "public_stats": (),
    "intelligence": (),
    "identity": ("ARGUS_SECURITY__SESSION_SECRET", "ARGUS_SECURITY__MFA_ENCRYPTION_KEY"),
    "telegram": ("TELEGRAM_WEBHOOK_SECRET",),
    "health": (),
    "ingestion": ("FMP_API_KEY",),
    "scanner": ("FMP_API_KEY",),
    "telegram_dispatch": ("TELEGRAM_BOT_TOKEN",),
    "retention": (),
}

#: The four settings the IaC DSL has no field for, as data. Each entry is
#: (setting, which processes need it, why it matters).
UNSUPPORTED_BY_IAC: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "dockerfile",
        tuple(PROCESSES),
        "Without it Railway falls back to Railpack, which builds a different "
        "image: no non-root user, no postgresql-client, so backup.py's "
        "pg_dump and pg_restore are missing from the running container.",
    ),
    (
        "preDeployCommand",
        ("identity",),
        "The migration has to run before traffic reaches new code, and a "
        "non-zero exit has to abandon the deploy. Run from the start command "
        "instead, a failed migration is a crash loop, not a stopped deploy.",
    ),
    (
        "cronSchedule",
        tuple(name for name, process in PROCESSES.items() if process.schedule),
        "Without a schedule Railway treats the service as a long-running "
        "process, restarts it every time it exits, and the job runs "
        "continuously instead of once.",
    ),
    (
        "restartPolicy",
        tuple(PROCESSES),
        "Railway's default restarts forever. A container that cannot start "
        "then looks busy rather than broken.",
    ),
)


def dashboard_settings(name: str) -> dict[str, Any]:
    """The settings for one process that IaC cannot carry.

    Set these on the Railway service itself — dashboard, CLI, or API. This
    function is the reference for what they should be; nothing applies them.
    """
    process = PROCESSES[name]
    settings: dict[str, Any] = {
        "builder": "DOCKERFILE",
        "dockerfilePath": DOCKERFILE_PATH,
        "restartPolicyType": RESTART_POLICY,
        "restartPolicyMaxRetries": RESTART_MAX_RETRIES,
    }
    if name == "identity":
        settings["preDeployCommand"] = [PRE_DEPLOY_COMMAND]
    if process.schedule:
        settings["cronSchedule"] = process.schedule
    return settings


def _env_lines(name: str, indent: str) -> list[str]:
    """One service's `env` block.

    `ARGUS_ENV` is derived from the Railway environment rather than written
    down per service, because writing it down per service is how two of the
    three live services ended up running the development profile in
    production. Anything that is not the production environment gets the
    staging profile: it is the conservative direction to be wrong in, since
    staging still refuses plaintext and still forbids wildcard CORS.
    """
    lines = [
        f'{indent}ARGUS_ENV: prod ? "production" : "staging",',
        f"{indent}DATABASE_URL: db.env.DATABASE_URL,",
    ]
    if PROCESSES[name].is_web:
        # See processes.py: the rate limiter's counters are per process, so
        # N workers is N independent ceilings. config.py refuses to start a
        # production process with more than one and no shared store.
        lines.append(f'{indent}WEB_CONCURRENCY: "1",')
    if name in {"ingestion", "scanner"}:
        # Both raise ScannerNotReady rather than guessing a universe, and
        # both read the same variable through the same function — bars
        # ingested for one universe and coverage measured against another
        # would be a silent, very confusing failure.
        lines.append(f"{indent}ARGUS_UNIVERSE_VERSION: preserve(),")
    for variable in SECRET_VARIABLES[name]:
        lines.append(f"{indent}{variable}: preserve(),")
    return lines


def _service_block(name: str, process: ProcessDefinition) -> list[str]:
    identifier = name if name != "identity" else "identityService"
    lines = [
        f"  // {process.description}",
        f"  const {identifier} = service({_ts(SERVICE_NAMES[name])}, {{",
        f'    source: github("{GITHUB_REPO}", {{ branch: "{GITHUB_BRANCH}" }}),',
        f"    start: {_ts(process.command())},",
    ]
    if process.is_web:
        lines.append(f"    healthcheck: {_ts(process.health_path)},")
        lines.append(f"    healthcheckTimeout: {HEALTHCHECK_TIMEOUT_SECONDS},")
        lines.append("    replicas: 1,")
    lines.append("    env: {")
    lines.extend(_env_lines(name, "      "))
    lines.append("    },")
    lines.append("  });")
    return lines


def _ts(value: str) -> str:
    """A TypeScript string literal. Double quotes, because the commands
    contain single ones."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _header() -> list[str]:
    unsupported = [
        f"//   - {setting}: {', '.join(processes)}" for setting, processes, _ in UNSUPPORTED_BY_IAC
    ]
    return [
        "// ARGUS — Railway Infrastructure as Code.",
        "//",
        "// GENERATED FILE. Regenerate with `python -m infra.deploy.railway`.",
        "// The definitions live in infra/deploy/processes.py; edit there.",
        "//",
        "// This file is NOT a complete deployment. The IaC DSL has no field",
        "// for four settings ARGUS needs, which are set on the Railway",
        "// services themselves — see dashboard_settings() in",
        "// infra/deploy/railway.py for the exact values:",
        *unsupported,
        "//",
        "// Secrets appear here as names bound to preserve(), never as values.",
        "// preserve() keeps what Railway already holds; it cannot create a",
        "// secret, so each one must exist on the service before the first",
        "// apply that mentions it.",
        "//",
        "// Omit means delete. Every service in this environment must appear",
        "// below, Postgres included. Run `railway config plan` and read the",
        "// destructive lines before `railway config apply` — an unexpected",
        "// delete is a bug in the generator, not an instruction.",
        "",
        'import { defineRailway, github, postgres, preserve, project, service } from "railway/iac";',
        "",
        "export default defineRailway((ctx) => {",
        '  const prod = ctx.environment === "production";',
        "",
        "  // Managed Postgres. Declared so that apply does not read its",
        "  // absence as an instruction to destroy it and its volume.",
        f"  const db = postgres({_ts(POSTGRES_SERVICE)});",
        "",
    ]


def render() -> str:
    """The full `.railway/railway.ts`."""
    lines = _header()
    identifiers = ["db"]
    for name, process in PROCESSES.items():
        lines.extend(_service_block(name, process))
        lines.append("")
        identifiers.append(name if name != "identity" else "identityService")

    lines.append(f"  return project({_ts(RAILWAY_PROJECT)}, {{")
    lines.append(f"    resources: [{', '.join(identifiers)}],")
    lines.append("  });")
    lines.append("});")
    return "\n".join(lines) + "\n"


def write(path: Path | None = None) -> Path:
    """Write the generated file. Returns the path written."""
    target = path or IAC_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render())
    return target


if __name__ == "__main__":  # pragma: no cover - regeneration by hand
    print(write())
