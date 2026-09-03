# Deployment runbook

Everything ARGUS needs to run somewhere other than a laptop, and the
reasoning behind each choice. Module 25.

This document is the operator-facing half of `infra/deploy/`; the
modules alongside it carry the reasoning for their own mechanisms. Where
the two overlap, the code is authoritative — `.railway/railway.ts` is
*generated* from `processes.py`, and a test fails if the committed file
drifts from it.

> **Superseded, 2026-09.** An audit of the live Railway project found that
> the seven `railway.json` files this document used to describe had never
> been read: every deployed service was building with Railpack, because
> nothing on Railway pointed at `infra/deploy/railway/*.json`. Railway has
> since deprecated Config as Code entirely — existing files stop being read
> on **2026-12-01** and new services cannot opt in. Those files are deleted.
> The replacement is Infrastructure as Code, described in §9.

---

## 1. What gets deployed

Seven processes from one image. `infra/deploy/processes.py` is the single
definition; this table is a reading of it.

| Service        | Kind | Command                                          | Health check   | Schedule       |
| -------------- | ---- | ------------------------------------------------ | -------------- | -------------- |
| `terminal`     | web  | `uvicorn infra.deploy.asgi:terminal_app`         | `/health/live` | —              |
| `public_stats` | web  | `uvicorn infra.deploy.asgi:public_stats_app`     | `/health/live` | —              |
| `intelligence` | web  | `uvicorn infra.deploy.asgi:intelligence_app`     | `/health/live` | —              |
| `identity`     | web  | `uvicorn infra.deploy.asgi:identity_app`         | `/health/live` | —              |
| `health`       | web  | `uvicorn infra.deploy.asgi:health_app`           | `/health/live` | —              |
| `ingestion`    | cron | `python -m infra.deploy.ingestion`               | —              | `0 21 * * 1-5` |
| `telegram`     | web  | `uvicorn infra.deploy.asgi:telegram_app`         | `/health/live` | —              |
| `scanner`      | cron | `python -m infra.deploy.scanner`                 | —              | `30 22 * * 1-5` |
| `telegram_dispatch` | cron | `python -m infra.deploy.telegram_dispatch`  | —              | `0 23 * * 1-5` |
| `retention`    | cron | `python -m infra.deploy.retention`               | —              | `0 3 * * *`    |

Only `identity` carries `preDeployCommand`. See §4.

`telegram` is the one service needing a variable beyond the connection
string: `TELEGRAM_WEBHOOK_SECRET`, without which it refuses to start in
staging or production. That refusal is deliberate — see
`services/telegram/README.md` — and
`tests/integration/deploy/test_platform_environment.py` asserts both
halves of it.

### One image, ten commands

The API services and the Live Scanner run **the same image**. That was
measured rather than assumed: importing the four service apps pulls in
pandas and numpy transitively through the feature and scoring layers, so
a "slim API image" without the analytical stack does not exist to build.
Two images would therefore differ by a `postgresql-client` package and
nothing else, in exchange for two build pipelines, two caches, and the
possibility of the scanner and the API running different code.

One image also means one git SHA answers "what produced this row",
which is the question this project's lineage design exists to answer.

The runtime stage installs `postgresql-client` deliberately:
`infra/deploy/backup.py` shells out to `pg_dump` and `pg_restore`, and a
recovery tool that is only present on a laptop is not a recovery tool.

### The ASGI server

`uvicorn[standard]`, run with `--factory`. FastAPI needs an ASGI server
to serve anything and the repository had none until this module —
Modules 19 through 22 built apps that only ever ran under `TestClient`.
`[standard]` brings `httptools` and `uvloop`, which is the difference
between a production server and a development one.

`--factory` because each `*_app` function builds its own engine and
validates the deployment profile at call time. A module-level `app`
object would run that at import, which turns a misconfiguration into an
import error in whatever process happens to import the module first,
including a test collector.

`--no-access-log` because uvicorn's access log writes one unstructured
line per request in its own format, alongside Module 23's JSON. Request
observability belongs to Module 23, in Module 23's shape.

`--workers ${WEB_CONCURRENCY:-1}`, defaulting to one. See §8.

---

## 2. Environments

`ARGUS_ENV` selects the profile, exactly as Module 02 defined it:
`development`, `staging`, `production`. `infra/deploy/config.py` maps
each to a `DeploymentProfile`, and `DeploymentProfile.validate()` runs at
process startup and **raises** rather than warns.

|                          | development | staging     | production  |
| ------------------------ | ----------- | ----------- | ----------- |
| HSTS                     | off         | 1 day       | 1 year, +subdomains |
| Redirect/refuse plaintext| off         | on          | on          |
| Trusted proxies          | none        | private net | private net |
| Wildcard CORS            | permitted   | refused     | refused     |
| Shared rate-limit store required for >1 worker | no | yes | yes |

Staging's HSTS is one day and does not include subdomains. A year-long
HSTS header sent from a staging hostname by mistake makes that hostname
unreachable over plaintext for a year, on every browser that saw it —
staging is where a TLS mistake should be discoverable and reversible.

Development has no HSTS at all: browsers key HSTS by hostname, and
`localhost` is shared with every other project on the machine.

### Promotion

    dev (laptop) ──▶ staging (Railway) ──▶ production (Railway)

Same image, same commands, different `ARGUS_ENV` and different database.
A promotion is a redeploy of an image that has already run in staging;
nothing is rebuilt between the two, so nothing can differ between them
except configuration.

### Secrets

**A connection string is the whole database configuration.** `DATABASE_URL`
is resolved *before* `AppConfig` is validated, which matters because
`AppConfig` requires `database.port`, `database.name` and `database.user`
and a platform injects none of them. Validating first made the
supplied-URL path unreachable on exactly the platforms it exists for, and
that is not hypothetical — it is how the first Railway deploy crashed:

```
pydantic_core.ValidationError: 3 validation errors for AppConfig
database.port  Field required [input_value={'host': 'localhost'}]
database.name  Field required
database.user  Field required
  File "/app/infra/deploy/asgi.py", line 98, in build_engine
```

Fixed in `infra/db/connection.py`; `tests/integration/deploy/test_platform_environment.py`
boots every entrypoint from a connection string and nothing else, so it
cannot come back.


Railway environment variables → `EnvironmentSecretsProvider` → the
existing `SecretsProvider` chain. No new mechanism. `.railway/railway.ts`
names the variables each service needs but binds every secret one to
`preserve()`, which means "keep whatever Railway already holds" — so the
file can be reviewed in a pull request without becoming a second place a
credential lives or leaks. `test_no_secret_value_is_written_into_the_file`
enforces that: any line whose name looks secret-shaped must carry
`preserve()` and nothing else.

`preserve()` cannot *create* a secret. Each one has to exist on the service
before the first apply that mentions it.

The variables a deployment sets:

| Variable                     | Required | Notes |
| ---------------------------- | -------- | ----- |
| `ARGUS_ENV`                  | yes      | Selects the profile. |
| `DATABASE_URL`               | yes      | Railway injects it. Normalized to the `psycopg` driver, and **sufficient on its own** — none of the `ARGUS_DATABASE__*` fields are needed alongside it. |
| `FMP_API_KEY`                | yes      | Read only through `SecretsProvider`. |
| `ARGUS_CORS_ORIGINS`         | yes in staging/production | Comma-separated. Empty in production means no browser origin is allowed, which is a working state, not a broken one. |
| `ARGUS_UNIVERSE_VERSION`     | yes for `scanner` | Label or id. The scanner refuses to guess — see `scanner.py`. |
| `WEB_CONCURRENCY`            | no       | Defaults to 1. >1 without a shared rate-limit store refuses to boot. |
| `ARGUS_RATE_LIMIT_STORE_URL` | no       | Not yet consumed by a limiter. See §8. |
| `ARGUS_TRUSTED_PROXIES`      | no       | Overrides the profile default. |

---

## 3. The health check

Railway will not route traffic to a container whose health check does not
pass. That is what makes `/health/live` a deployment-safety mechanism
rather than a monitoring one, and it is why every web service answers it.

Only the `health` service has that route of its own, from Module 24. The
other four answer through `infra/deploy/liveness.py`, wrapped on at
composition time — without which Railway would poll a 404 and never route
traffic to any of them. The middleware checks whether the app already
serves the path and delegates if it does, so the health service keeps
answering with its own.

A **down** verdict (database unreachable, schema at the wrong revision)
returns 503 and the container is kept out of rotation. A **degraded**
verdict returns 200 and stays in: a stale data feed is not fixed by
having fewer servers, and removing them makes the outage worse. That is
Module 24's decision, unchanged.

Verdicts are cached for three seconds so a poll storm across ten
containers does not become a steady background query load.

---

## 4. Deploying

    1. push          → Railway builds the image from Dockerfile
    2. preDeploy     → python -m infra.deploy.migrate      (identity only)
    3. start         → containers boot, run validate(), open pools
    4. health check  → /health/live must pass
    5. route         → traffic moves to the new containers
    6. old stop

**Migrations run at step 2, before any traffic reaches new code.** A
non-zero exit abandons the deploy and the old containers keep serving.
That ordering is the entire safety property.

Exactly one service owns the step. Seven services running the same
migration concurrently would race, and Alembic's version table is not a
lock. `identity` owns it because it is the service whose schema every
other service's authentication depends on: if its migration fails, the
correct outcome is that nothing else deploys either.

### What running migrations first does *not* buy

New code never sees an old schema. Old code **does** see the new one —
during steps 3 to 6 the previous containers are still serving against an
already-migrated database. So every migration must be backwards
compatible with the code still running, and `assert_backwards_compatible`
checks that rather than trusting it.

Checking found the assumption already broken: **four of ARGUS's thirteen
migrations are not backwards compatible.** 0004, 0006 and 0008 tighten a
column to `NOT NULL`; 0007 does that and drops a unique constraint. None
has ever mattered, because ARGUS has never been deployed. All of them
matter from the first deploy onwards.

A first deploy is exempt, and legitimately: against a database with no
`alembic_version` row, there is no previous code running against a schema
that does not exist, so the property being protected is not in play. Any
later destructive migration must be split across two deploys — ship the
code that stops using the old shape first — or forced for one deploy with
`ARGUS_ALLOW_DESTRUCTIVE_MIGRATION=1`.

---

## 5. Rolling back

> **Rollback reverts the image, never the schema.**

Railway redeploys a previous build. The database stays where it is. That
works for exactly the reason §4 describes: a migration that is safe for
old code *during* a deploy is equally safe for old code *after* a
rollback.

Before rolling back, check the target:

    python -m infra.deploy.rollback 0012

Exit `0` means the old image can run against the schema as it stands.
Exit `1` means a destructive migration sits between the target and head,
and redeploying that image alone will not work.

`alembic downgrade` is **not** the recovery path. Every `downgrade()` in
this project drops tables or columns; running one destroys the data the
new schema was holding rather than restoring anything.
`rollback_schema()` exists, refuses by default, and says so:

> Refusing to downgrade the schema to 0012. Rolling back ARGUS means
> redeploying the previous image […] A downgrade from here would drop
> data rather than restore it — the recovery path for that is a restore
> from the pre-deploy backup.

If a destructive migration genuinely has to come out, the sequence is:
restore the pre-migration backup to a new database, point the old image
at it, and treat the forward migration as a bug to fix. Take that backup
*before* any migration that had to be forced.

### Rollback drill, performed

Against a throwaway database migrated to head (`0013`):

| Step | Result |
| ---- | ------ |
| `assess_rollback("0003")` | `code_rollback_safe=False`, blocking `0004, 0006, 0007, 0008` |
| `assess_rollback("0012")` | `code_rollback_safe=True`, blocking none |
| `rollback_schema("0012")` with no override | `SchemaRollbackRefused` |
| `rollback_schema("0012", env={ARGUS_ALLOW_SCHEMA_ROLLBACK: "1"})` | revision `0013 → 0012` |
| `to_regclass('registration_attempts')` after the downgrade | `NULL` — **the table and its rows are gone** |

The last line is the point. The downgrade "succeeded" and destroyed a
table. That is why the default is a refusal.

---

## 6. Backup and disaster recovery

### Three classes of data, with different recovery costs

| Class | Tables | Recoverable how | Cost |
| ----- | ------ | --------------- | ---- |
| **Reproducible** | `canonical_ohlcv`, `canonical_fundamentals`, `canonical_corporate_actions`, `canonical_news` | Re-fetch from FMP | Hours to days of API budget |
| **Recomputable** | features, market states, signals, setups, similarity results | Re-run from canonical data at a recorded lineage | Days — a 15-year scan is the long pole |
| **Irreplaceable** | `users`, `roles`, `sessions`, watchlists, `audit_log`, `login_attempts`, `registration_attempts`, `historical_scan_status`, `public_release_windows`, `setup_outcomes` | Restore only | Unrecoverable if the backup is bad |

`IRREPLACEABLE_TABLES` in `backup.py` is that third row, enumerated, and
`verify_restore` fails a restore that is missing any of them.

`setup_outcomes` is in the third class rather than the second because it
carries the human review classification (`review_confidence`,
`false_positive_type`) that Module 15 added. The numbers recompute; the
judgement does not.

### RPO: 24 hours

Set by a daily logical dump, and stated with its consequence rather than
without: **up to a day of registrations, logins, watchlist edits and
audit records can be lost.**

The reasoning is tied to what ARGUS actually guarantees. Its published
guarantee is point-in-time correctness of signals — and a signal is
reproducible from canonical data plus a recorded lineage, so class 1 and
class 2 have no meaningful RPO at all; they have a recompute cost. The
only data with a real recovery point is class 3, and its highest-volume
member is an audit trail that ARGUS is under no regulatory obligation to
retain to the minute.

A tighter RPO means continuous WAL archiving and point-in-time recovery,
which is a platform capability rather than something `pg_dump` can be
scheduled into. If ARGUS ever takes on a retention obligation — a real
user base, an auditor, a paying customer — 24 hours is no longer
defensible and PITR becomes mandatory. That is recorded in §8 rather than
quietly assumed away.

### RTO: 1 hour to a serving system; days to a complete one

**1 hour** covers: provision a database, restore the archive, run
`verify_restore`, redeploy the images. The measured restore in the drill
below was under a second, but that number scales with data and index
rebuild time, not with wall clock optimism — which is exactly why
`retention.py`'s growth threshold is set at 1 GiB per append-only table.
That threshold is an RTO control, not a disk-space one.

**Days**, honestly, to a system with its derived data back. Restoring
class 3 gives working authentication, a valid audit trail, and the
published track record. It does not give signals: those need canonical
data re-fetched and a full historical scan re-run. A DR plan that claimed
one hour for that would be a plan nobody could execute.

### The restore drill, performed

Not described — run, as one command, so anyone can run it again:

    python -m infra.deploy.backup

`drill()` derives its target from the source name plus `DRILL_SUFFIX` and
creates it; it does not accept a destination URL, because a restore drill
that takes a destination is a restore drill that can be aimed at
production by a typo. It refuses outright if the two names come out
equal. Exit `0` means the restored copy verified usable, `1` means it did
not, `2` means the drill could not run — a failing drill that has to be
noticed by reading output is a drill nobody notices failing.

Source `argus_m25_demo`, seeded through real ARGUS code paths
(`accounts.register`, `accounts.log_in`, `SecurityIdentityResolver`).
Actual output:

```json
{
  "source": "argus_m25_demo",
  "target": "argus_m25_demo_restore_drill",
  "passed": true,
  "backup": { "bytes": 153076, "seconds": 0.17 },
  "restore_seconds": 0.49,
  "verification": {
    "usable": true,
    "schema_revision": "0013", "expected_revision": "0013",
    "tables_present": 40,
    "missing_irreplaceable": [],
    "guard_triggers": 44, "expected_guard_triggers": 44,
    "row_counts": {
      "users": 3, "roles": 3, "sessions": 1, "audit_log": 6,
      "login_attempts": 3, "registration_attempts": 3,
      "user_watchlists": 0, "user_watchlist_items": 0,
      "historical_scan_status": 0, "public_release_windows": 0,
      "setup_outcomes": 0
    },
    "seconds": 0.52
  }
}
```

Then, on the **restored copy**, proving it is a usable system rather than
a set of matching row counts:

| Check | Result |
| ----- | ------ |
| `DELETE FROM audit_log` | `RestrictViolation` — guards restored, not merely present |
| `TRUNCATE audit_log` | `RestrictViolation` |
| `UPDATE login_attempts SET reason = 'x'` | `RestrictViolation` |
| `accounts.log_in`, correct password | session `9518dd26…` issued |
| `accounts.log_in`, wrong password | refused, `INVALID_CREDENTIALS` |
| Module 23 `check_health` | `degraded` / HTTP 200 — database ok, migrations ok at `0013`, data_freshness degraded (4 of 4 feeds stale) |

The degraded freshness verdict is correct and is part of the evidence:
the drill seeded no canonical data, so the feed genuinely is stale. A
restore that reported everything green there would mean the health check
was not looking.

#### One finding from the drill, worth recording

The first run reported `usable: false` on a guard-trigger count of 42
against an expected 44 — and the restore was perfect. The verification
query was wrong. It read `information_schema.triggers`, which reports one
row per *event* (so `BEFORE UPDATE OR DELETE` counts twice) and omits
TRUNCATE triggers entirely, because the SQL standard has no TRUNCATE. The
two errors nearly cancelled, which is the worst possible outcome for a
check: the number looked almost right. `verify_restore` now counts from
`pg_trigger` joined to `pg_proc`, and reports 44 of 44.

A second finding, from making the drill a command: `drill()` first passed
`str(build_database_url())` to `pg_dump`, and `URL.__str__` masks the
password — by design, so a logged URL cannot leak a credential. The
subprocess received `***` and the failure surfaced as
`password authentication failed`, which reads like a misconfigured
environment rather than like its actual cause. `_dsn()` now does that
conversion in one place, and the masking stays exactly as it was.

### Where the archive goes — the honest gap

`backup.py` writes to a path. On Railway that path is on an ephemeral
container filesystem, so a backup written there is not a backup. **ARGUS
has no configured off-platform archive destination**, and this module did
not invent one.

The scheduled backup path is therefore the managed database provider's
own snapshots, which must be verified as enabled before go-live.
`backup.py` covers the two cases a provider snapshot does not: the
operator-initiated dump taken immediately before a forced migration, and
the restore drill that proves any of it works. Closing this properly —
`dump()` streaming to object storage on a schedule, with a restore drill
run against the real archive — is the first item in §8.

---

## 7. Retention

`infra/deploy/retention.py`, enforced by the `retention` cron.

| What | Retention | Enforced by |
| ---- | --------- | ----------- |
| Application logs | Platform window | Railway. ARGUS does not control it. |
| `sessions` | 30 days past expiry | `prune_expired_sessions` |
| `audit_log` | Forever | Append-only trigger; growth monitored |
| `login_attempts` | Forever | Append-only trigger; growth monitored |
| `registration_attempts` | Forever | Append-only trigger; growth monitored |

Logs are JSON on stderr and nowhere else. The decision that follows, and
it constrains the rest of the system: **application logs are diagnostic
and disposable.** Anything that must outlive the platform's window goes
to `audit_log`. If something is only ever logged, it is not retained.

The three append-only tables cannot be pruned. A `DELETE` against any of
them raises SQLSTATE 23001 from a trigger, and dropping the guard to run
a retention job would destroy the property the table exists for — a
lockout whose evidence can be deleted is not a lockout. So retention
there is measurement: `measure_growth` reports rows, on-disk bytes, the
observed arrival rate, and days until the 1 GiB threshold. That turns
"grows unbounded" into a number with a date on it.

The migration path, when that date arrives, is declarative partitioning
by month — guards stay on the parent, old data leaves by `DETACH
PARTITION` rather than `DELETE`. Deliberately not implemented now: it is
a schema change to three tables on behalf of a projection, in a database
with zero production rows.

---

## 8. Before this could safely go live

Written as a list of things that are **not done**, not as caveats.

1. **Off-platform backup storage.** `dump()` writes to a path; nothing
   schedules it and nothing ships the archive anywhere durable. Until
   this exists, DR depends entirely on the provider's snapshots, which
   have not been verified as enabled or restorable. This is the largest
   gap on the list.
2. **The image builds and runs — one command of ten is proven.**
   Module 25 wrote this entry as "never built": Docker Hub egress was
   blocked in the build environment (403 on
   `production.cloudfront.docker.com`), so nothing could be verified
   locally. Railway has since built and run it, which settles the
   question the other way. Deployment `6435838a`'s log carries frames at
   `/app/infra/deploy/asgi.py` and `/opt/venv/.../uvicorn/` — the exact
   paths this Dockerfile creates — with uvicorn invoking `terminal_app`
   through `--factory`, which is `processes.py`'s generated start
   command. Image, venv, source layout and start command are all
   confirmed by that.

   What is *not* confirmed is the other nine commands. `terminal_app`
   is the only factory a real container has ever executed.
3. **Nothing after startup is confirmed.** That deploy crashed at
   configuration resolution — the `DATABASE_URL` ordering bug, fixed and
   regression-tested by `test_platform_environment.py`. A crash at
   startup proves the build and says nothing about the running system.
   TLS termination, the platform's own health-check polling, the
   pre-deploy migration hook, the cron schedules, and whether all ten
   process definitions exist as ten Railway services or one, all
   remain as documented and unverified.

   The lesson is worth keeping separately from the bug: a suite of 2,204
   tests passed while the deployed process could not start, because every
   test that touched configuration built one and handed it in. Nothing
   ran the path a container runs. `test_platform_environment.py` is now
   that path, and any future configuration source should be added to it
   before it is added anywhere else.
4. **The rate limiter is single-process.** `WEB_CONCURRENCY` is 1 and a
   production process refuses to boot with more, which contains the
   problem rather than solving it. Horizontal scaling — replicas, not
   just workers — multiplies every configured ceiling by the replica
   count. A shared store (Redis) is required before scaling past one
   container per service. `ARGUS_RATE_LIMIT_STORE_URL` is read but
   nothing consumes it yet.
5. **Four ordering hazards are live.** Catalogued in
   `infra/observability/ordering.py`, monitored, unfixed — Module 25 was
   explicitly told to carry them forward. Any "latest row wins" query
   over rows written in one transaction can return either row.
6. **No PITR.** RPO is 24 hours because a daily dump is the mechanism. A
   real user base makes that indefensible.
7. **No load test.** Pool sizes (10 + 5 overflow per process), the
   120-second health-check timeout and the 3-second liveness cache are
   reasoned, not measured.
8. **The scanner has never run in production shape.** `run_scheduled_scan`
   is tested; a real weekday firing against a real universe version with
   real FMP data has not happened. Module 25's boundary forbade it.

---

## 9. Infrastructure as Code

`.railway/railway.ts`, generated by `python -m infra.deploy.railway` from the
same `PROCESSES` table §1 reads. `railway config plan` previews it against a
linked environment; `railway config apply` commits it after confirmation.

### Why the format changed

Config as Code (`railway.json`, `railway.toml`) is deprecated: new services
cannot opt into it and existing files stop being read on **2026-12-01**. That
alone would force the move. The audit finding is the more useful half of the
reason: the seven files were never read *at all*. Every service was building
with Railpack, which produces a different image from the one §1 describes —
no non-root user, and no `postgresql-client`, so `backup.py`'s `pg_dump` and
`pg_restore` were absent from the running container the whole time.

A committed configuration the platform does not read is not configuration.
It is a comment that looks authoritative, and it passed a test suite for
months. The new tests are written to be harder to satisfy vacuously, but the
durable lesson is the one from §8: nothing here is verified until something
outside the repository has read it.

### What the file cannot say

The IaC DSL documents `source`, `build`, `start`, `healthcheck`,
`healthcheckTimeout`, `replicas`, `env`, `domains`, `volumeMounts`,
databases, buckets and groups. It has no field for four settings this
deployment depends on:

| Setting                    | Needed by              | If it is missing |
| -------------------------- | ---------------------- | ---------------- |
| Dockerfile builder + path  | every service          | Railpack builds a different image |
| `preDeployCommand`         | `identity`             | A failed migration is a crash loop, not an abandoned deploy |
| `cronSchedule`             | `ingestion`, `scanner`, `telegram_dispatch`, `retention` | The job runs continuously instead of once |
| Restart policy and retries | every service          | A container that cannot start looks busy rather than broken |

`UNSUPPORTED_BY_IAC` in `infra/deploy/railway.py` is that table as data and
`dashboard_settings(name)` renders the values; the tests assert both. Nothing
applies them. They are set per service on Railway, by hand or through the
API, and that manual step is now the single most forgettable part of a
deployment. Treat a new service as unfinished until it has all four.

### Omit means delete

IaC reads the file as the whole environment. A service that exists on Railway
and is absent from `railway.ts` is one `apply` offers to destroy — which is
why the managed Postgres is declared there despite this repository owning
none of its configuration, and why `SERVICE_NAMES` tracks the names services
actually carry today rather than the names they should have.

Two of those names are wrong and are deliberately left wrong: `"Argus"`
serves the Terminal, and `"Identity "` has a trailing space. Renaming them in
`railway.ts` would not rename them on Railway; it would destroy each service
and create a replacement, losing its variables and its deployment history.
Rename in the dashboard first, then update `SERVICE_NAMES` to match.

Read every destructive line in a plan. An unexpected delete is a bug in the
generator, not an instruction.

### Live state, 2026-09-02

Three of the ten processes are deployed: `terminal` (as `Argus`),
`public_stats` and `identity`. `intelligence`, `health`, `ingestion`,
`scanner`, `telegram`, `telegram_dispatch` and `retention` have no
Railway service at all — so nothing has ever been ingested, no scan has
ever been scheduled, no alert has ever been sent and no session has ever
been pruned.

`ingestion` (Module 26) is new and is the one whose absence matters most.
The other four missing services degrade what ARGUS can *show*; this one
is what fills `canonical_ohlcv`, and without it the scanner has nothing
to scan even once it exists. Create it before `scanner`, not after —
and note that on the numbers as they stand today the scanner still
cannot see what it writes, for the reason set out in
`core/ingestion/README.md`. All three live services now
build from the Dockerfile, run the production profile, and carry the
health check, restart policy and — on `identity` — the pre-deploy migration.
That was applied through the API, not from this file; the first
`railway config plan` should therefore report little or nothing to change on
those three, and four services to create.
