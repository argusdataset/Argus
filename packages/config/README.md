# Config

Configuration — built in Module 02. The single place other modules get
typed, validated settings from. See `packages/config/__init__.py` for the
public API: `get_config()`, `Environment`, `ExecutionMode`,
`SecretsProvider`, `get_secrets_provider()`.

## Structure

`AppConfig` (in `settings.py`) is grouped by domain, not one flat blob:

- `environment` — `development` / `staging` / `production`, selected via
  the `ARGUS_ENV` env var (defaults to `development`; an unrecognized
  value fails loudly instead of silently falling back).
- `database` — connection settings Module 03 will use. No password field
  — see Secrets below.
- `providers` — FMP settings Module 04 will use. No API key field — see
  Secrets below.
- `execution` — the `ExecutionMode` (`live` / `historical_batch`) switch
  Modules 08-16 will read. See
  `docs/architecture/CROSS_CUTTING_REQUIREMENTS.md` (section A) for why
  this is defined before anything consumes it.
- `logging` — level/format fields. Module 23 wires up the actual logging
  framework.
- `secrets` — settings *about* secret resolution (e.g. where the local
  `.env` file is), never a secret value itself.

Nested fields are read from env vars using a double-underscore delimiter,
e.g. `ARGUS_DATABASE__PORT`. Every env var uses the `ARGUS_` prefix except
`ARGUS_ENV` itself, which has no `ARGUS_ENVIRONMENT` alias.

## Usage

```python
from packages.config import get_config

config = get_config()
config.database.port
config.execution.mode
```

`get_config()` loads and validates `AppConfig` once per process (cached).
Missing required settings raise `pydantic.ValidationError` immediately,
naming the missing field — never a `None` that surfaces confusingly three
layers deep later.

## Secrets

No secret value (database password, FMP API key, ...) is ever a field on
`AppConfig`. All secret access goes through `SecretsProvider`
(`secrets.py`):

```python
from packages.config import get_secrets_provider

secrets = get_secrets_provider()
api_key = secrets.get_secret("FMP_API_KEY")
```

Today the only implementation is `DotEnvSecretsProvider`, which reads from
a local `.env` file — development use only. A real secrets manager (AWS
Secrets Manager, Vault, ...) is a later infrastructure decision; adding one
means adding a new `SecretsProvider` implementation here, not touching
every module that resolves a secret.

## Adding a new setting

1. Add the field to the relevant group in `settings.py` (or a new group,
   if it's a genuinely new domain). Give it a default if one is safe
   across every environment; leave it required if not.
2. Document the env var in `.env.example`.
3. Add a test under `tests/unit/config/`.

## Adding a new secret

1. Add the key (blank/dummy value) to `.env.example`, in the secrets
   section at the bottom.
2. Read it via `get_secrets_provider().get_secret("YOUR_KEY")` at the point
   of use — never add it as an `AppConfig` field.

## Local dev setup

```bash
cp .env.example .env
# fill in ARGUS_DATABASE__*, and any secrets you actually need, in .env
```

`.env` is gitignored; only `.env.example` (with blank/dummy values) is
committed.
