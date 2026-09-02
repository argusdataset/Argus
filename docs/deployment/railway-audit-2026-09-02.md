# Railway deployment audit — 2026-09-02

What the live Railway project actually looked like, what was changed, what
was verified, and what is still wrong. Written from a session that had read
access to the repository and read/write access to the Railway API, so every
claim below is either a quoted tool response or a quoted deploy log. Where
something was not verified, it says so.

The short version: `infra/deploy/README.md` §8 said the deployment was
unverified. It was worse than unverified — the configuration this repository
committed had never been read by the platform at all.

---

## 1. Method

- Repository read from a clone of `argusdataset/Argus` at `bcda3fa`.
- Railway read through the Railway MCP connector against project
  `passionate-unity` (`8a4269b5-…`), environment `production`
  (`0df54336-…`).
- Runtime behaviour confirmed by fetching the public service domains
  directly, not only by reading Railway's own status fields.
- Variable **values** were never read; the audit works from variable names
  and from what each process logged at startup, which is enough because
  `asgi.py` logs the resolved profile.

---

## 2. Baseline — what was found

### 2.1 Two projects, four services, seven expected

```txt
passionate-unity   (8a4269b5-1544-4d7c-9d4b-9e6d568e3ad8)
  Argus            -> terminal      d4bad10f… (name says nothing about that)
  Public_stats     -> public_stats  a798f215…
  "Identity "      -> identity      d27c3a6a… (trailing space in the name)
  Postgres                          c2304b46…

diplomatic-adventure (3cee6364-…)
  Argus            offline, no deployments, no variables — abandoned
```

`PROCESSES` defines seven. Four had no Railway service at all:
`intelligence`, `health`, `scanner`, `retention`. So no scan had ever been
scheduled and no session had ever been pruned — not "the scanner has never
run in production shape" as §8 put it, but *there was nothing to run it*.

### 2.2 The committed configuration was never read

Every service:

```json
"build": { "builder": "RAILPACK", "buildEnvironment": "V3" }
```

Nothing pointed at `infra/deploy/railway/*.json`. Railway looks for a config
file at the repository root by default, and no service had the path
overridden. Consequences, all of them real rather than theoretical:

- The image was Railpack's, not the `Dockerfile`'s. No non-root user. No
  `postgresql-client` — so `backup.py`'s `pg_dump` and `pg_restore`, the
  disaster-recovery tooling the Dockerfile installs *specifically* so that it
  is not "only present on a laptop", were absent from every running
  container.
- `Argus` had **no start command at all**. Railpack inferred one. It
  happened to start the Terminal.
- Only `Public_stats` and `Identity ` had health check paths.

Attempting to set the config file path returned:

```txt
Config as Code (railway.json / railway.toml) is deprecated.
Use Infrastructure as Code (.railway/railway.ts) instead.
```

So the mechanism the repository was built around is not merely unused; it is
withdrawn. Existing files stop being read on **2026-12-01** and new services
cannot opt in.

### 2.3 Two services ran the development profile in production

From the deploy logs, `argus.deploy.asgi` / `service_composed`:

```txt
Argus         environment=development  hsts=false  trusted_proxies=0
Identity      environment=development  hsts=false  trusted_proxies=4
Public_stats  environment=production   hsts=true   trusted_proxies=4
```

The development profile does not enforce HTTPS, sends no HSTS, and permits
wildcard CORS. Both affected services were on public domains. The more
serious of the two is `Identity`, the only service in ARGUS that issues
credentials.

This is the failure mode of writing `ARGUS_ENV` down once per service: it
only takes one copy to be wrong, and nothing compares the copies.

### 2.4 The migration safety property was not in effect

`identity`'s start command was:

```sh
sh -c 'python -m infra.deploy.migrate && uvicorn infra.deploy.asgi:identity_app --factory …'
```

with `preDeployCommand: []`.

README §4 describes migrations running at step 2, before traffic reaches new
code, with a non-zero exit abandoning the deploy — "that ordering is the
entire safety property". Run from the start command instead, a failing
migration is a crash loop, and the migration runs once per container rather
than once per deploy.

### 2.5 Interactive API documentation was public

```txt
GET https://joyful-insight-production-059f.up.railway.app/docs
  -> "ARGUS Identity API - Swagger UI"
```

No service passes `openapi_url=None` to `FastAPI(...)`, so `/docs` and
`/openapi.json` are served by default. The full endpoint map of registration,
sign-in, MFA and admin routes was readable by anyone.

### 2.6 Other findings

- Deploy source is the branch `claude/new-session-96aymh`, not `main`.
- `ARGUS_CORS_ORIGINS` is set on no service (`cors_origins=0` everywhere).
  Per README §2 that is "a working state, not a broken one", but it means no
  browser origin can call any API.
- `Public_stats` carries three variables only; it has no `FMP_API_KEY`.
- `Identity ` sets `PORT` manually. Railway injects `PORT` itself — the two
  services without it also bind 8080.
- `ARGUS_SECRETS__DOTENV_PATH=.env` is set in production. It is a
  local-development setting.
- Deployment history: 12 failures on `Identity `, 4 on `Argus`, 2 on
  `Public_stats`.

---

## 3. Changes applied

All through the Railway API. Nothing was applied to `diplomatic-adventure`.

### 3.1 Profile

```txt
set-variables  Argus       ARGUS_ENV=production
set-variables  Identity    ARGUS_ENV=production
```

`ARGUS_TRUSTED_PROXIES` was also written explicitly on `Argus`
(`10.0.0.0/8,100.64.0.0/10,172.16.0.0/12,fd00::/8`). It equals the production
profile default, so it changes no behaviour; it was set to trigger a fresh
deployment (see §3.3) and to make the value visible next to the other two
services, which already carried it.

Checked before applying, because the same change had previously caused an
outage: `config.py`'s production profile defaults `trusted_proxies` to
`RAILWAY_PRIVATE_NETWORK`, which includes `100.64.0.0/10` since
`9fcb8b2`. Without that range, `require_https=True` produces an infinite
redirect (`ERR_TOO_MANY_REDIRECTS`), which is exactly how the first
`public_stats` production deploy failed.

### 3.2 Build and deploy settings

Per service, matching what `infra/deploy/processes.py` defines:

```txt
update-service  Argus         dockerfilePath=Dockerfile
                              startCommand="sh -c 'uvicorn infra.deploy.asgi:terminal_app --factory
                                --host 0.0.0.0 --port ${PORT:-8000}
                                --workers ${WEB_CONCURRENCY:-1} --no-access-log'"
                              healthcheckPath=/health/live  healthcheckTimeout=120
                              restartPolicyType=ON_FAILURE  restartPolicyMaxRetries=10

update-service  Identity      (same, identity_app)
                              preDeployCommand=["python -m infra.deploy.migrate"]
                              startCommand no longer contains the migration

update-service  Public_stats  (same, public_stats_app)

set-variables   Identity      WEB_CONCURRENCY=1
set-variables   Public_stats  WEB_CONCURRENCY=1
```

Setting `dockerfilePath` moved `build.builder` from `RAILPACK` to
`DOCKERFILE` on all three.

### 3.3 One thing worth keeping: redeploy does not pick up new build settings

The first attempt to apply the new builder used Railway's redeploy. It
failed:

```txt
using build driver railpack-v0.38.0
  ↳ Detected Python
  ✖ No start command detected.
railpack prepare exited with an error
```

Redeploy re-runs a deployment with the **build configuration that deployment
was created with**. The service config had already changed; the deployment
had not. A *new* deployment is required, which a variable write triggers.
The service stayed up throughout on its previous deployment.

Operationally: after changing build settings, do not press Redeploy. Cause a
new deployment.

---

## 4. Verification

Not asserted — observed.

### 4.1 The Dockerfile is now the image

```txt
[builder  3/13] RUN python -m venv /opt/venv
[runtime  2/6]  RUN apt-get install --no-install-recommends -y libpq5 postgresql-client
[runtime  4/6]  RUN useradd --create-home --uid 10001 argus
```

`pg_dump` and `pg_restore` are in the running containers for the first time.

### 4.2 All three run the production profile

```txt
service=terminal      environment=production hsts=true trusted_proxies=4 workers=1
service=identity      environment=production hsts=true trusted_proxies=4 workers=1
service=public_stats  environment=production hsts=true trusted_proxies=4 workers=1
```

### 4.3 The migration runs before the app, in its own container

```txt
Starting Container
  alembic.runtime.migration  Context impl PostgresqlImpl.
  argus.deploy.migrate       schema already at head  revision=0013
Stopping Container
Starting Container
  argus.deploy.asgi          service_composed  service=identity  environment=production
```

Two containers, in order. That is the property §2.4 said was missing.

### 4.4 Nothing broke

```txt
GET https://argus-production-32f4.up.railway.app/health/live      -> {"status":"up"}
GET https://joyful-insight-production-059f.up.railway.app/health/live -> {"status":"up"}
GET https://publicstats-production.up.railway.app/health/live     -> {"status":"up"}
```

No redirect loop. Deployments `9ca006d7`, `f46cea57`, `8b3ea057` all SUCCESS.

---

## 5. Not done, and why

Three categories, kept separate because they need different things.

**Not expressible through the API used here.** The connector has no operation
for these; they are dashboard work:

| Item | Note |
| ---- | ---- |
| Delete `PORT` on `Identity ` | Variables can be written, not removed. Value is 8080, so harmless — but it should not be there. |
| Verify Postgres backups | Backup schedule is not exposed by the API. **Nothing else covers DR right now**, so this is the highest-value item on this list. |
| Delete `diplomatic-adventure` | No project or service deletion operation. |
| Change deploy branch to `main` | Source changes are out of scope for `update-service`. Alternatively let `.railway/railway.ts` do it — it pins `main`. |
| Rename `Argus` → `terminal`, `"Identity "` → `identity` | Renaming through IaC destroys and recreates. Dashboard first, then `SERVICE_NAMES`. |

**Blocked on information only the owner has.**

| Item | What is needed |
| ---- | -------------- |
| `ARGUS_CORS_ORIGINS` | The frontend origin. Guessing it would ship a wrong value that looks deliberate. |
| `FMP_API_KEY` on `Public_stats` | Whether that service needs it at all. |

**Deliberately deferred.**

- The four missing services. Creating them is straightforward; running them
  is not free, and the workspace showed **$4.55 / 17 days** of credit
  remaining at the time of the audit. Seven services will consume that
  faster than three.
- `openapi_url=None`. A code change, not a Railway one — see §6.

---

## 6. Remaining work, in order

1. **Verify Postgres backups are enabled.** Until this is confirmed there is
   no disaster recovery at all: `backup.py` writes to an ephemeral container
   filesystem, which README §6 already names as the largest gap.
2. **Close `/docs` and `/openapi.json`**, at minimum on `identity`. One
   argument to `FastAPI(...)` per service.
3. **Land `.railway/railway.ts` on `main`** and point the services at `main`.
   The generated file pins `main`; applying it before the branch carries the
   code would point every service at a branch without it.
4. **Rename the two mis-named services**, then update `SERVICE_NAMES`.
5. **Remove `PORT` and `ARGUS_SECRETS__DOTENV_PATH`** from the production
   services.
6. **Set `ARGUS_CORS_ORIGINS`** once the frontend origin exists.
7. **Create `intelligence`, `health`, `scanner`, `retention`** — after
   credit. Each needs the four settings IaC cannot carry
   (`dashboard_settings()` renders them), and `scanner` additionally needs
   `ARGUS_UNIVERSE_VERSION` or it will refuse to run.
8. **Delete `diplomatic-adventure`.**

Unchanged from README §8 and not addressed here: off-platform backup
storage, the single-process rate limiter, the four ordering hazards, PITR,
and load testing.

---

## 7. Current state, verbatim

`passionate-unity` / `production`, after the changes.

```jsonc
// Argus  (terminal)
{
  "source": { "repo": "argusdataset/Argus", "branch": "claude/new-session-96aymh" },
  "build":  { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": {
    "startCommand": "sh -c 'uvicorn infra.deploy.asgi:terminal_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --no-access-log'",
    "healthcheckPath": "/health/live",
    "healthcheckTimeout": 120
  },
  "domains": ["argus-production-32f4.up.railway.app", "argus-production-c47b.up.railway.app"]
}

// Identity   (identity — note the trailing space in the service name)
{
  "source": { "repo": "argusdataset/Argus", "branch": "claude/new-session-96aymh" },
  "build":  { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": {
    "startCommand": "sh -c 'uvicorn infra.deploy.asgi:identity_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --no-access-log'",
    "preDeployCommand": ["python -m infra.deploy.migrate"],
    "healthcheckPath": "/health/live",
    "healthcheckTimeout": 120
  },
  "domains": ["joyful-insight-production-059f.up.railway.app"]
}

// Public_stats
{
  "source": { "repo": "argusdataset/Argus", "branch": "claude/new-session-96aymh" },
  "build":  { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": {
    "startCommand": "sh -c 'uvicorn infra.deploy.asgi:public_stats_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --no-access-log'",
    "healthcheckPath": "/health/live",
    "healthcheckTimeout": 120
  },
  "domains": ["publicstats-production.up.railway.app"]
}

// Postgres
{
  "source": { "image": "ghcr.io/railwayapp-templates/postgres-ssl:18" },
  "volumeMounts": { "/var/lib/postgresql/data": "…" },
  "schemaRevision": "0013"
}
```

Two services still do not exist as anything, and two more names still lie
about what they run. Both are in §6.
