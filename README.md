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

This repository is being built **module by module**. This is **Module 01**:
the architecture and project skeleton. It contains no business logic, no
database schema, no external API calls, and no data — just the structure
everything else will be built into. See
[`docs/architecture/ARGUS_CONTEXT.md`](docs/architecture/ARGUS_CONTEXT.md)
for the full project context.

## Repository structure

```
argus/
├── core/                              # Intelligence Core — one deployable unit
│   ├── universe/                      # Universe definition & filtering
│   ├── data_validation/               # Canonical data validation
│   ├── feature_engine/                # Feature vector computation
│   ├── candidate_detection/           # Setup-phase candidate detection
│   ├── target_model_matching/         # Target model matching
│   │   └── models/target_model_v1/    # First named target model
│   ├── historical_similarity/         # Historical setup similarity search
│   ├── risk_context/                  # Risk / volatility / liquidity context
│   ├── scoring/                       # Explainable multi-component scoring
│   ├── lifecycle/                     # Setup lifecycle tracking
│   ├── outcome_tracking/              # Outcome recording (nothing deleted)
│   └── model_validation_evaluation/   # Model validation & evaluation
│       ├── validation/
│       └── evaluation/
├── services/
│   ├── terminal/                      # Fundamentals/news, independent of scoring
│   ├── watchlist/                     # User watchlists
│   ├── public_stats/                  # Public track-record stats
│   └── identity/                      # Auth/users
├── data/
│   ├── provider_adapters/
│   │   └── fmp/                       # FMP provider adapter
│   ├── canonical_model/               # Canonical schema + validation
│   └── normalization/                 # Corp actions, ticker/identity history
├── packages/
│   ├── model_registry_client/         # Shared model registry client
│   ├── feature_schema/                # Shared feature-vector schema
│   └── shared_types/                  # Shared types across core/services
├── infra/
│   ├── db/                            # DB migrations (Module 03)
│   └── observability/                 # Logging/metrics/tracing config
├── docs/
│   └── architecture/                  # ARGUS_CONTEXT.md and other design docs
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
```

Every leaf folder has its own `README.md` stub noting what future module
will fill it in, and an `__init__.py` marking it as a Python package.

## Language & tooling

- **`core/` and `data/`**: Python — this is where feature engineering,
  pattern matching, and other quantitative work will live, and it needs the
  pandas/numpy/scikit-learn ecosystem down the line.
- **`services/`**: Python as well, for MVP simplicity (no second
  language/runtime until there's a concrete reason to add one). The eventual
  API framework will be **FastAPI** — no endpoints exist yet.
- **Package manager**: plain `pip` + a PEP 621 `pyproject.toml` (not Poetry).
  At this stage there are no runtime dependencies and nothing is published,
  so Poetry's lockfile/resolver machinery doesn't buy anything yet; this can
  be revisited once `core/` and `data/` pick up real dependencies worth
  pinning.
- **Linting/formatting**: [`ruff`](https://docs.astral.sh/ruff/) for both
  lint and format, configured in `pyproject.toml`.
- **Pre-commit**: `.pre-commit-config.yaml` runs ruff lint + format on every
  commit.
- **CI**: `.github/workflows/ci.yml` installs dependencies, lints, and runs
  the (currently empty) test suite. Nothing else — no deployment, no
  database, no external services.

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

# 5. Run the (currently empty) test suite
pytest
```

## Status

This is **Module 01 of a module-by-module build**. Nothing in this
repository makes a network call, touches a database, or contains real
business logic — see `docs/architecture/ARGUS_CONTEXT.md` for the full
system context and module map.
