# ARGUS

ARGUS is a financial intelligence system for US equities. It continuously
scans a 10,000+ stock universe for one specific structural setup — a stock
that fell hard, stabilized, based in a range for an unpredictable amount of
time, and is now showing signs of waking up into a sustained rally — and
tries to surface candidates **during the base, before the rally starts**.
Every score it produces is broken into named, explainable components (pattern
match quality, volatility compression, volume structure, historical
similarity, liquidity, risk), backed by historical evidence of how similar
setups played out — including every time they failed, which is never
deleted. ARGUS surfaces evidence and probabilities; it does not make trading
decisions and is not (yet) a commercial product — the near-term goal is to
run it honestly for years and build a real track record first.

This repository was built **module by module**, and all 27 planned modules
are now built (deployment to Railway is in progress — see
[Status](#status)). See
[`docs/architecture/ARGUS_CONTEXT.md`](docs/architecture/ARGUS_CONTEXT.md)
for the full project context, and
[`docs/architecture/KNOWN_ISSUES.md`](docs/architecture/KNOWN_ISSUES.md) for
the audited, currently-open gaps (below in [Status](#status)).

## Module map

| # | Module | Lives in |
|---|--------|----------|
| 01 | Architecture & project foundation | repository skeleton |
| 02 | Configuration, environments & secrets | `packages/config/` |
| 03 | Database foundation | `infra/db/` |
| 04 | FMP provider adapter | `data/provider_adapters/fmp/` |
| 05 | Canonical data model & normalization | `data/canonical_model/`, `data/normalization/` |
| 06 | Universe construction & versioning | `core/universe/` |
| 07 | Point-in-time enforcement | `core/data_validation/` |
| 08 | Feature engineering engine | `core/feature_engine/` |
| 09 | Candidate detection & eligibility gating | `core/candidate_detection/` |
| 10 | Market state engine & target-model-v1 | `core/market_state/` |
| 11 | Historical similarity engine | `core/historical_similarity/` |
| 12 | Risk context, pending events, invalidation | `core/risk_context/` |
| 13 | Scoring engine | `core/scoring/` |
| 14 | Setup lifecycle (event-sourced) | `core/lifecycle/` |
| 15 | Outcome tracking, MFE/MAE, CASE record | `core/outcome_tracking/` |
| 16 | Explanation layer | `core/explanation/` |
| 17 | Replay, evaluation, publish gating | `core/model_validation_evaluation/` |
| 18 | The live scanner, scheduled | `core/live_scanner/` |
| 19 | Terminal API | `services/terminal/` |
| 20 | Public page & public-stats gate | `services/public_stats/` |
| 21 | Intelligence API | `services/intelligence/` |
| 22 | Authentication & user management | `services/identity/` |
| 23 | Monitoring, logging & observability | `infra/observability/` |
| 24 | Security hardening | `infra/security/` |
| 25 | Production deployment & disaster recovery | `infra/deploy/` |
| 26 | Daily ingestion orchestration & deep refresh | `core/ingestion/` |
| 27 | Telegram alerts on BREAKOUT_READY | `services/telegram/` |

A Phase 1 Integration Audit followed Module 25 and produced
[`docs/architecture/KNOWN_ISSUES.md`](docs/architecture/KNOWN_ISSUES.md) (also
available as a PDF, `docs/architecture/PHASE1_AUDIT_REPORT.pdf`) — the single
register of what each module's own report flagged as unfixed, plus what the
audit itself found. Entries leave that register by being fixed and having the
fix pointed at, not by being forgotten.

## Repository structure

```
argus/
├── core/                              # Intelligence Core — one deployable unit
│   ├── universe/                      # Module 06 — universe definition & versioning
│   ├── data_validation/               # Module 07 — point-in-time enforcement
│   ├── feature_engine/                # Module 08 — feature vector computation
│   ├── candidate_detection/           # Module 09 — setup-phase candidate detection
│   ├── market_state/                  # Module 10 — market state engine, target-model-v1, watchlists
│   ├── historical_similarity/         # Module 11 — historical setup similarity search
│   ├── risk_context/                  # Module 12 — risk / volatility / liquidity context
│   ├── scoring/                       # Module 13 — explainable multi-component scoring
│   ├── lifecycle/                     # Module 14 — setup lifecycle tracking (event-sourced)
│   ├── outcome_tracking/              # Module 15 — outcome recording (nothing deleted)
│   ├── explanation/                   # Module 16 — explanation layer
│   ├── model_validation_evaluation/   # Module 17 — replay, evaluation, publish gating
│   │   ├── validation/
│   │   └── evaluation/
│   ├── live_scanner/                  # Module 18 — the scheduled scanner
│   └── ingestion/                     # Module 26 — daily ingestion orchestration
├── services/
│   ├── terminal/                      # Module 19 — fundamentals/news, independent of scoring
│   ├── watchlist/                     # User watchlists (query layer over core/market_state)
│   ├── public_stats/                  # Module 20 — public track-record page & stats gate
│   ├── intelligence/                  # Module 21 — intelligence API
│   ├── identity/                      # Module 22 — auth/users
│   ├── telegram/                      # Module 27 — BREAKOUT_READY alerts (webhook + dispatch)
│   └── shared/                        # Cross-service helpers
├── data/
│   ├── provider_adapters/
│   │   └── fmp/                       # Module 04 — FMP provider adapter
│   ├── canonical_model/               # Module 05 — canonical schema + validation
│   └── normalization/                 # Corp actions, ticker/identity history
├── packages/
│   ├── config/                        # Module 02 — configuration, environments & secrets
│   ├── model_registry_client/         # Shared model registry client
│   ├── feature_schema/                # Shared feature-vector schema
│   └── shared_types/                  # Shared types across core/services
├── infra/
│   ├── db/                            # Module 03 — schema & migrations
│   ├── observability/                 # Module 23 — logging/metrics/tracing
│   ├── security/                      # Module 24 — security hardening
│   └── deploy/                        # Module 25 — Docker, Railway IaC, cron process defs
├── docs/
│   └── architecture/                  # ARGUS_CONTEXT.md, KNOWN_ISSUES.md, and other design docs
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
```

## Language & tooling

- **`core/` and `data/`**: Python, with the pandas/numpy ecosystem for the
  quantitative work (feature engineering, pattern matching, scoring).
- **`services/`**: Python as well, all served over HTTP by **FastAPI**
  (`services/terminal`, `services/public_stats`, `services/intelligence`,
  `services/identity`, `services/telegram`) under a single ASGI entrypoint,
  `infra/deploy/asgi.py`.
- **Database**: PostgreSQL via SQLAlchemy 2.x, migrated with Alembic
  (`infra/db/`).
- **Package manager**: plain `pip` + a PEP 621 `pyproject.toml` (not Poetry) —
  see the note at the bottom of `pyproject.toml` for why.
- **Linting/formatting**: [`ruff`](https://docs.astral.sh/ruff/) for both
  lint and format, configured in `pyproject.toml`.
- **Pre-commit**: `.pre-commit-config.yaml` runs ruff lint + format on every
  commit.
- **CI**: `.github/workflows/ci.yml` installs dependencies, lints, and runs
  the test suite.
- **Deployment**: Railway, one service per module boundary (scanner,
  ingestion, telegram webhook + dispatch, terminal, public_stats,
  intelligence, identity, retention, health) plus managed Postgres — see
  `infra/deploy/README.md` and `.railway/railway.ts`.

## Dev environment setup

**Prerequisite: PostgreSQL 16.** The schema depends on Postgres-specific
features — plpgsql triggers enforcing append-only history, native enum
types, JSONB, GiST exclusion constraints and partial unique indexes — so
an older major version or a different engine will not run the migrations.
The database-backed tests skip cleanly when no server is reachable.

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install the project in editable mode with dev dependencies
pip install -e ".[dev]"

# 3. (optional) install the pre-commit hook
pre-commit install

# 4. Lint
ruff check .
ruff format --check .

# 5. Run migrations against a local Postgres, then the test suite
alembic -c infra/db/alembic.ini upgrade head
pytest
```

## Status

All 27 modules are built, with a deployable Docker image and Railway IaC
covering every one of them (see `infra/deploy/`). Deployed is a narrower
claim, and the number below is checked directly against Railway
(`environment-status`, `list-services`) rather than copied from
`infra/deploy/README.md`'s own "Live state" section — that section is
prose, goes stale the moment a new service is created, and did: an
earlier version of this file's Status section repeated a three-of-ten
count from it that was already out of date by two modules' worth of
services. As of 2026-09-04, checked directly: nine of the ten processes
run on Railway and report zero issues — `terminal` (as `Argus`),
`public_stats`, `identity`, `intelligence`, `health` and `telegram`
online, `scanner`, `telegram_dispatch` and `retention` ready as cron.
Only **`ingestion`** (Module 26) has no Railway service yet — see
`infra/deploy/README.md`'s "Live state" section for detail, but verify
against Railway directly before repeating a count from it again. A
Phase 1 Integration Audit has run against the deployment
(`docs/architecture/KNOWN_ISSUES.md`). What's tracked there as open, as of
the audit and the fixes since:

- **Corporate actions are never ingested** (splits/dividends) — `docs/architecture/KNOWN_ISSUES.md`
  G2. Until this ships, an unadjusted split or dividend can distort a
  security's price history silently.
- **`get_config()` cannot be loaded in the deployed environment** — G3,
  still open and **not yet exercised in production**. Every deployed
  service runs on a bare `DATABASE_URL`; one code path
  (`infra/db/connection.py`) already works around it, but any other caller
  of `get_config()` — including Module 26's ingestion cron — still
  crashes. Untested against real traffic only because `ingestion` is the
  one process this would hit and it has not been created on Railway yet;
  expect it to surface the moment it is.
- **The scanner cannot see the bars ingestion writes** — G1, **resolved**:
  `scan_offset_hours` and `readiness_window_hours` in
  `core/live_scanner/config.py` were misaligned with the bar-availability
  lag and with each other; both are fixed, which also unblocks the
  BREAKOUT_READY watchlist and the Module 27 Telegram bot's alerts. Not
  yet meaningful in production, though: `canonical_ohlcv` is still empty
  with `ingestion` unbuilt, so a passing readiness check has nothing to
  say yes to.
- **No universe has been built or set** in production yet
  (`ARGUS_UNIVERSE_VERSION`), and ingestion needs a paid FMP API key — until
  both are in place, there is nothing for the scanner to scan regardless of
  G1/G3.

See `docs/architecture/KNOWN_ISSUES.md` for the complete, audited list
(severity, who found each item, and what fixing it looks like) rather than
relying on this summary going stale again.
