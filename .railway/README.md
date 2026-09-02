# Railway configuration

`railway.ts` is the Infrastructure as Code file for the ARGUS project. It is
**generated**. Do not edit it by hand — edit `infra/deploy/processes.py` (the
process definitions) or `infra/deploy/railway.py` (the topology and the
rendering), then regenerate:

    python -m infra.deploy.railway

`tests/unit/deploy/test_railway_config.py` fails if the committed file drifts
from what the generator produces now.

## Working with it

    npm install                 # once, inside this directory
    railway login
    railway link                # pick the project and environment
    railway config plan         # preview — read every destructive line
    railway config apply        # only after the plan is what you expect

`plan` is safe; it only reads. `apply` is not. IaC treats this file as the
whole environment: **a service that exists on Railway and is missing here is
a service `apply` offers to destroy.** An unexpected delete in a plan is a
bug in the generator, not an instruction — fix the generator.

## This file is not the whole deployment

The IaC DSL has no field for four settings ARGUS depends on:

| Setting                    | Needed by              |
| -------------------------- | ---------------------- |
| Dockerfile builder + path  | every service          |
| `preDeployCommand`         | `identity`             |
| `cronSchedule`             | `scanner`, `retention` |
| Restart policy and retries | every service          |

They are set on the Railway services themselves. `dashboard_settings(name)`
in `infra/deploy/railway.py` renders the exact values, and the tests assert
them, but nothing applies them — that is manual, per service, and it is the
step whose omission left every service building with Railpack.

## Changing a service's name

Renaming a service here does not rename it on Railway. It destroys one
service and creates another, taking its variables and its deployment history
with it. Rename in the Railway dashboard first, then update `SERVICE_NAMES`.
