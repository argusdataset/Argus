// ARGUS — Railway Infrastructure as Code.
//
// GENERATED FILE. Regenerate with `python -m infra.deploy.railway`.
// The definitions live in infra/deploy/processes.py; edit there.
//
// This file is NOT a complete deployment. The IaC DSL has no field
// for four settings ARGUS needs, which are set on the Railway
// services themselves — see dashboard_settings() in
// infra/deploy/railway.py for the exact values:
//   - dockerfile: terminal, public_stats, intelligence, identity, health, scanner, retention
//   - preDeployCommand: identity
//   - cronSchedule: scanner, retention
//   - restartPolicy: terminal, public_stats, intelligence, identity, health, scanner, retention
//
// Secrets appear here as names bound to preserve(), never as values.
// preserve() keeps what Railway already holds; it cannot create a
// secret, so each one must exist on the service before the first
// apply that mentions it.
//
// Omit means delete. Every service in this environment must appear
// below, Postgres included. Run `railway config plan` and read the
// destructive lines before `railway config apply` — an unexpected
// delete is a bug in the generator, not an instruction.

import { defineRailway, github, postgres, preserve, project, service } from "railway/iac";

export default defineRailway((ctx) => {
  const prod = ctx.environment === "production";

  // Managed Postgres. Declared so that apply does not read its
  // absence as an instruction to destroy it and its volume.
  const db = postgres("Postgres");

  // Module 19. Company fundamentals, news, chart data, user watchlists.
  const terminal = service("Argus", {
    source: github("argusdataset/Argus", { branch: "main" }),
    start: "uvicorn infra.deploy.asgi:terminal_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --no-access-log",
    healthcheck: "/health/live",
    healthcheckTimeout: 120,
    replicas: 1,
    env: {
      ARGUS_ENV: prod ? "production" : "staging",
      DATABASE_URL: db.env.DATABASE_URL,
      WEB_CONCURRENCY: "1",
      FMP_API_KEY: preserve(),
    },
  });

  // Module 20. Public, unauthenticated track record. Expects real traffic.
  const public_stats = service("Public_stats", {
    source: github("argusdataset/Argus", { branch: "main" }),
    start: "uvicorn infra.deploy.asgi:public_stats_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --no-access-log",
    healthcheck: "/health/live",
    healthcheckTimeout: 120,
    replicas: 1,
    env: {
      ARGUS_ENV: prod ? "production" : "staging",
      DATABASE_URL: db.env.DATABASE_URL,
      WEB_CONCURRENCY: "1",
    },
  });

  // Module 21. Derived watchlists, per-security detail, case explanations.
  const intelligence = service("intelligence", {
    source: github("argusdataset/Argus", { branch: "main" }),
    start: "uvicorn infra.deploy.asgi:intelligence_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --no-access-log",
    healthcheck: "/health/live",
    healthcheckTimeout: 120,
    replicas: 1,
    env: {
      ARGUS_ENV: prod ? "production" : "staging",
      DATABASE_URL: db.env.DATABASE_URL,
      WEB_CONCURRENCY: "1",
    },
  });

  // Module 22. The only service that issues credentials.
  const identityService = service("Identity ", {
    source: github("argusdataset/Argus", { branch: "main" }),
    start: "uvicorn infra.deploy.asgi:identity_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --no-access-log",
    healthcheck: "/health/live",
    healthcheckTimeout: 120,
    replicas: 1,
    env: {
      ARGUS_ENV: prod ? "production" : "staging",
      DATABASE_URL: db.env.DATABASE_URL,
      WEB_CONCURRENCY: "1",
      ARGUS_SECURITY__SESSION_SECRET: preserve(),
      ARGUS_SECURITY__MFA_ENCRYPTION_KEY: preserve(),
    },
  });

  // Modules 23/24. Public liveness plus the admin-gated detailed view. Its own liveness path is the one Railway polls.
  const health = service("health", {
    source: github("argusdataset/Argus", { branch: "main" }),
    start: "uvicorn infra.deploy.asgi:health_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --no-access-log",
    healthcheck: "/health/live",
    healthcheckTimeout: 120,
    replicas: 1,
    env: {
      ARGUS_ENV: prod ? "production" : "staging",
      DATABASE_URL: db.env.DATABASE_URL,
      WEB_CONCURRENCY: "1",
    },
  });

  // Module 18. One catch-up scan per weekday evening.
  const scanner = service("scanner", {
    source: github("argusdataset/Argus", { branch: "main" }),
    start: "python -m infra.deploy.scanner",
    env: {
      ARGUS_ENV: prod ? "production" : "staging",
      DATABASE_URL: db.env.DATABASE_URL,
      ARGUS_UNIVERSE_VERSION: preserve(),
      FMP_API_KEY: preserve(),
    },
  });

  // Module 25. Prunes expired sessions; measures append-only growth.
  const retention = service("retention", {
    source: github("argusdataset/Argus", { branch: "main" }),
    start: "python -m infra.deploy.retention",
    env: {
      ARGUS_ENV: prod ? "production" : "staging",
      DATABASE_URL: db.env.DATABASE_URL,
    },
  });

  return project("passionate-unity", {
    resources: [db, terminal, public_stats, intelligence, identityService, health, scanner, retention],
  });
});
