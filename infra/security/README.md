# Security Hardening (Module 24)

Works through a list, not from scratch. Modules 22 and 23 each did a
narrow amount of security work and named what they deliberately left for
later — ten items from Module 22, three from Module 23. This module's
job was to resolve every one of those thirteen, explicitly, and to
re-verify a handful of things across the whole project (secrets, log
scrubbing) rather than assume they still held after two modules' worth
of new code.

## Files

| File | What it owns |
|---|---|
| `client_ip.py` | Trusted-proxy `X-Forwarded-For` resolution. Off by default |
| `config.py` | Trusted proxies, CORS origins, rate ceilings — all `security`-kind |
| `rate_limit.py` | The general per-source request ceiling, at the ASGI layer |
| `headers.py` | CORS wiring and the standard security response headers |
| `middleware.py` | `harden(app)` — one call wiring all of the above onto a service |
| `health_app.py` | `/health/live` (public) and `/health/detail` (admin-gated) |
| `scrubbing.py` | The project-wide AST log-scrubbing scan |
| `secrets_audit.py` | The re-run secrets audit, as code rather than a claim |

## The consolidated list, resolved

| # | Item | Resolution | Why |
|---|---|---|---|
| 1 | `X-Forwarded-For` trust gap | **Fixed** | `client_ip.py` — trusts the header only from a configured peer; unconfigured falls back to `request.client.host`, Module 22's original behaviour |
| 2 | No TLS enforcement | **Deferred** | Infrastructure/deployment concern — see below |
| 3 | Tokens in a header, not a cookie | **Deferred** | Correct for today's API-only surface; a browser UI needs a different design, noted, not built |
| 4 | No session rotation on privilege change | **Fixed** | `accounts.change_role()` ends every session and requires re-authentication |
| 5 | `needs_rehash` unused | **Fixed** | Wired into `log_in`, strictly after verification succeeds |
| 6 | No per-IP registration limit | **Fixed** | `registration_attempts` (migration 0013) + `registration_lockout_state`, counting every attempt in the window |
| 7 | No account recovery / MFA scope | **Confirmed deferred** | A feature-scope decision, not hardening — unchanged from Module 22 |
| 8 | Commit-on-refusal transaction rule | **Re-read, undisturbed** | Still exactly as Module 22 built it |
| 9 | Unbounded append-only tables | **Deferred** | An operations/retention policy, not a code defect |
| 10 | AST log-scrubbing scoped to two packages | **Fixed** | `scrubbing.py` + `tests/unit/security/test_log_scrubbing_project_wide.py` — one scan, five roots |
| 11 | Unauthenticated health endpoint | **Fixed** | Split: `/health/live` (up/down, no detail) vs `/health/detail` (`admin` + MFA) |
| 12 | No log retention/rotation | **Deferred** | Operations decision, Module 25-adjacent |
| 13 | Four ordering hazards | **Confirmed, not fixed** | `probe()` re-verified; one new safe entry added for Module 24's own table; the four hazards and `probe()` itself untouched |

Also addressed, from the project's original security architecture:
general API rate limiting (`rate_limit.py`, all four services), CORS and
standard security headers (`headers.py`, all four services), and the
secrets re-audit (`secrets_audit.py`).

## The general rate limiter is deliberately the blunt instrument

Module 22's brute-force protection is precise, persistent, and
identity-aware: it counts failed logins per email and per source,
survives a restart, and is exactly right for credential guessing.
`RateLimitMiddleware` is none of those things on purpose — an in-memory,
per-process, fixed-window burst ceiling on raw request volume, sitting in
front of every route including ones with no other protection at all. It
exists to catch a runaway client before it does real work, not to be a
forensic record. **Single-process only**: running more than one worker
gives each its own counter, so the effective ceiling multiplies with
process count. Closing that needs a shared store (Redis) reachable from
every process — an infrastructure decision, flagged for Module 25.

## Health: liveness and detail are different audiences

`/health/live` answers one question — is the process able to talk to its
database — with one word, unauthenticated, because an orchestrator asking
"should I restart this" needs exactly that and nothing about it changes
based on which feed is stale or what schema revision is running.
`/health/detail` answers everything `check_health` knows, gated behind
`admin` and an enrolled second factor — the same gate `services/identity`
already established, called directly rather than reimplemented.

## The secrets audit runs, not narrates

`run_secrets_audit()` re-scans the repository for a hardcoded credential,
confirms `.env`/`.env.*` are gitignored with none besides `.env.example`
tracked, and confirms no environment secret read bypasses
`SecretsProvider` outside one documented, named exception
(`infra/db/migrations/env.py`'s test-only URL override). All three pass
today; a test runs the same scan and would fail the day any of them stop.

## What this module does not do

- **No TLS termination, HSTS, or redirect-to-HTTPS.** `headers.py`
  explicitly does not set `Strict-Transport-Security` — that header is a
  promise about TLS this layer cannot keep on its own, and belongs beside
  whatever actually terminates TLS. Module 25's concern.
- **No cookie-based sessions, no CSRF machinery.** No browser UI exists
  yet; the design is noted in Module 22's own report, not built here.
- **No account recovery.**
- **No log retention or rotation policy.**
- **No fix for the four ordering hazards.** Confirmed live; the
  monotonic-sequence migration and the Module 11/15/17/21 changes it
  would require are exactly the open-ended refactor this module was told
  to avoid.
