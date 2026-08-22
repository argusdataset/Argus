# Database Foundation

Database Infra — built in Module 03. Schema definitions, Alembic
migrations, and the connection layer. Target is **PostgreSQL 16**.

No business logic lives here: no ingestion (Module 04), no canonical
translation (Module 05), no PIT enforcement (Module 07), no feature,
state, or scoring computation (Modules 08-13). This module provides the
fields and constraints those modules depend on.

## Layout

```
infra/db/
├── connection.py     # Engine/URL from packages.config + SecretsProvider
├── metadata.py       # Shared MetaData, naming convention, PIT column helper
├── enums.py          # Enumerated domains (Python <-> PostgreSQL enum types)
├── append_only.py    # Immutability guard definitions
├── schema/           # Table definitions, grouped by domain
│   ├── identity.py       # security_identity, ticker history, universe
│   ├── canonical.py      # OHLCV, fundamentals, corporate actions
│   ├── versioning.py     # version/config entities + feature_vectors
│   ├── intelligence.py   # market state, eligibility, signals, similarity, events
│   ├── setups.py         # setups, setup_events, setup_outcomes
│   ├── validation.py     # validation runs, evaluation reports, review gate
│   └── users.py          # users, roles, sessions, watchlists, audit, billing
├── alembic.ini
└── migrations/       # Alembic environment + versions/
```

Schema is defined with SQLAlchemy **Core** (`Table` objects), not the
declarative ORM. Alembic needs a `MetaData` target to diff against, and
Core provides exactly that; the ORM's mapper/session machinery would be
unused weight. Later modules that want ORM mapping can map onto these
tables without the schema being defined in ORM terms.

## Entity overview

**Identity & universe.** `security_identity` is the stable internal ID
that everything references — never a ticker, because tickers get
reassigned and recycled while identity does not.
`security_ticker_history` maps ticker to identity over validity ranges.
`universe_version` / `universe_membership` version the analyzable
universe. There is no fixed universe size anywhere in the schema, and
delisted/bankrupt securities are retained deliberately: dropping them is
exactly how survivorship bias enters a backtest.

**Canonical data.** `canonical_ohlcv` (raw *and* adjusted series),
`canonical_fundamentals`, `canonical_corporate_actions`. Every row
carries all four PIT timestamps, non-nullable. Restatements arrive as new
rows with a later `observation_time`, never as edits.

**Versioning.** `feature_schema_version`, `target_model_version`,
`scoring_configuration`, `detection_configuration`, `data_snapshot` —
all immutable. `feature_vectors` is stamped with the schema version that
defined its field names.

**Intelligence.** `market_state` (current projection) plus
`market_state_transitions` (append-only authority);
`eligibility_check_results`; `signals`; `historical_similarity_results`;
`pending_material_events`.

**Setups.** `setups`, `setup_events` (append-only), `setup_outcomes`
(the case record, including MFE/MAE and the review classification).

**Validation.** `model_validation_runs`, `model_evaluation_reports`,
`historical_scan_status` (the review gate).

**User domain.** `users`, `roles`, `sessions`, `user_watchlists`,
`user_watchlist_items`, `audit_log`, `entitlements`, `subscriptions`.

## The four PIT timestamps

Every canonical row carries four separate timestamps, all `NOT NULL`:

| Column | Meaning |
|---|---|
| `event_time` | when the real-world event occurred (bar close, fiscal quarter end) |
| `observation_time` | when the value was first observed/reported |
| `availability_time` | when it became available to ARGUS, accounting for provider lag |
| `ingestion_time` | when ARGUS actually stored it |

ARGUS's core claim is *"this pattern actually would have worked
historically."* That claim is worthless if a historical calculation
secretly used information that did not exist yet. Module 07 enforces the
rule — a query "as of X" may only see rows with `availability_time <= X`
— and this module makes it enforceable. **If these fields were collapsed
into one or left nullable, the guarantee could not be recovered later
without reprocessing everything**, which is why they are non-nullable and
why a test asserts it.

`availability_time` is indexed on every canonical table, and
`feature_vectors` carries its own `availability_time` (the latest across
all its inputs) so a vector's PIT-correctness can be verified without
re-deriving its input set.

## Append-only and immutability guarantees

Enforced by database triggers, not application convention. Two strengths:

**`APPEND_ONLY_TABLES` — reject UPDATE, DELETE and TRUNCATE.**
`universe_version`, `universe_membership`, `feature_schema_version`,
`target_model_version`, `scoring_configuration`,
`detection_configuration`, `data_snapshot`, `market_state_transitions`,
`eligibility_check_results`, `signals`, `setup_events`,
`historical_similarity_results`, `historical_scan_status`, `audit_log`.

**`NO_DELETE_TABLES` — reject DELETE and TRUNCATE, permit UPDATE.**
`setups`, `setup_outcomes`.

The asymmetry is deliberate. A failed setup must be impossible to erase,
but the human review classification (`review_confidence`,
`false_positive_type`) is genuinely assigned after the outcome is
computed and sometimes revised on re-review. Blocking UPDATE outright
would force that workflow into a supersession chain on a table that is
otherwise one row per setup.

Why these rules exist:

- **Failed setups are permanent record.** They are how the system learns
  what a false positive looks like, and keeping every one of them is
  what stops ARGUS's own published statistics from inheriting
  survivorship bias. A failed case carries identical data richness to a
  successful one.
- **Signals must stay reproducible.** A signal records the IDs of the
  configuration that produced it; re-running those IDs must give an
  identical result. A published configuration that can be silently
  edited invalidates every historical signal referencing it. Corrections
  publish a *new* version, or write a new `signals` row pointing at the
  one it supersedes.

Both guard strengths also block `TRUNCATE`, which does not fire row-level
triggers — a row-only guard would leave the whole table erasable by one
statement.

The migration freezes its own copy of the guard table lists, because
migrations are historical records that must not change behaviour when
`append_only.py` is later edited. `tests/integration/db/test_append_only.py`
asserts the guards actually installed still match that module, so drift
between the two is caught rather than silently tolerated.

These guards do not defend against a superuser dropping the trigger.
That is a database-permissions concern (Module 24), not a schema one.

## Connection

`connection.py` builds the URL from `packages.config` (Module 02):
host/port/name/user come from `DatabaseSettings`, and the password from
`SecretsProvider.get_secret("DATABASE_PASSWORD")`. The password is never
a config field, so it cannot appear in a config object's `repr`, a log
line, or a serialized settings dump. `build_database_url` returns a
SQLAlchemy `URL`, which masks the password in its own `repr`.

```python
from infra.db import create_db_engine

engine = create_db_engine()
```

No engine is created at import time and none is cached — the caller owns
the lifecycle, since a long batch run and an API process have very
different pooling needs.

## Migration workflow

Migrations read the database URL from `packages.config` at runtime, so no
credential is committed. `ARGUS_MIGRATION_DATABASE_URL` overrides it for
tests and one-off maintenance.

```bash
# Apply everything
alembic -c infra/db/alembic.ini upgrade head

# Roll back one revision / all the way
alembic -c infra/db/alembic.ini downgrade -1
alembic -c infra/db/alembic.ini downgrade base

# Create a new revision after editing infra/db/schema/
alembic -c infra/db/alembic.ini revision --autogenerate -m "describe change"

# Preview SQL without connecting
alembic -c infra/db/alembic.ini upgrade head --sql
```

Two things autogenerate will not do for you, both handled explicitly in
the initial migration and needing the same treatment in future ones:

1. **Enum types.** Several enums (notably `market_state_enum`) are used
   by more than one table. Autogenerate emits a `CREATE TYPE` per column,
   which fails on the second table, so types are created once up front
   and dropped in `downgrade`.
2. **Trigger guards.** Any new append-only table needs its guard
   installed in the migration and dropped before the tables are, or
   `DROP TABLE` is blocked during downgrade.

Downgrade is verified to leave nothing behind — no tables, no enum types,
no trigger function — and the migration is proven re-appliable afterwards.

## Running the tests

Database tests need a real PostgreSQL. They skip with a clear message if
none is reachable, and CI provides one as a service container.

```bash
# Local server on the default URL
pytest tests/integration/db

# Or point at your own
ARGUS_TEST_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/postgres pytest
```

Each run creates a throwaway database, migrates it to head, and drops it
afterwards.

## Scaling note

`canonical_ohlcv` and `feature_vectors` are the high-volume tables —
roughly a full US equity universe times ~15 years of daily bars. If
volume later demands it, these are the tables that would move to
**TimescaleDB** (a PostgreSQL extension) or **ClickHouse**. Nothing here
uses a construct that would block that migration, and access goes through
the connection layer rather than raw DSNs, so application code should not
need rewriting. **Neither dependency is added now** — this is a note
about keeping the option open, not a plan being executed.
